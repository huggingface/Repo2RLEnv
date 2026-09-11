"""Execute supplied family generators and reference programs in remote offline containers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import uuid
from pathlib import Path

from repo2rlenv.execution.job import container_labels
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.pipelines.recipes.scaler.compatibility import (
    fix_newlines_in_python_strings,
    import_needed_module_for_python,
)
from repo2rlenv.pipelines.recipes.scaler.families import (
    instruction_for,
    parse_testcase,
    scale_parameters,
)
from repo2rlenv.spec.recipe_options import ScalerOptions
from repo2rlenv.ui import console


def execute(
    image: str, code: str, language: int, stdin: str, directory: Path, timeout: int
) -> dict:
    directory.mkdir(parents=True)
    source = directory / "code"
    source.mkdir()
    (source / ("main.py" if language == 3 else "main.cpp")).write_text(code)
    (source / "input").write_text(stdin)
    name = "scaler-exec-" + uuid.uuid4().hex
    command = (
        ["python", "/code/main.py"]
        if language == 3
        else [
            "sh",
            "-c",
            "g++ -std=c++17 -O2 /code/main.cpp -o /tmp/program && /tmp/program < /code/input",
        ]
    )
    if language == 3:
        command = ["sh", "-c", "python /code/main.py < /code/input"]
    subprocess.run(
        [
            "docker",
            "create",
            *container_labels(),
            "--name",
            name,
            "--network",
            "none",
            "--cpus",
            "1",
            "--memory",
            "1g",
            "--pids-limit",
            "128",
            "--user",
            "1000:1000",
            image,
            *command,
        ],
        capture_output=True,
        timeout=30,
        check=True,
    )
    try:
        subprocess.run(
            ["docker", "cp", str(source), name + ":/code"],
            capture_output=True,
            timeout=30,
            check=True,
        )
        result = subprocess.run(
            ["docker", "start", "-a", name],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        state = json.loads(subprocess.check_output(["docker", "inspect", name], timeout=30))[0][
            "State"
        ]
        record = {
            "returncode": state["ExitCode"],
            "oom": state.get("OOMKilled", False),
            "stdout": result.stdout[:262144],
            "stderr": result.stderr[-16000:],
            "output_truncated": len(result.stdout) > 262144,
            "language": language,
            "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
        }
        save_record(directory / "result.json", record)
        return record
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30, check=False)


def generate(config: dict, destination: Path) -> dict:
    options = ScalerOptions.model_validate(config["options"])
    families = config["families"]
    destination.mkdir(parents=True, exist_ok=False)
    build = destination / "runtime"
    build.mkdir()
    dockerfile = "FROM python:3.12-slim\nRUN apt-get update && apt-get install -y --no-install-recommends g++ && rm -rf /var/lib/apt/lists/*\n"
    (build / "Dockerfile").write_text(dockerfile)
    tag = "repo2rlenv-scaler:" + hashlib.sha256(dockerfile.encode()).hexdigest()[:16]
    result = subprocess.run(
        ["docker", "build", "-t", tag, str(build)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    (build / "stdout.txt").write_text(result.stdout)
    (build / "stderr.txt").write_text(result.stderr)
    if result.returncode:
        raise ValueError("SCALER Python/C++ runtime image failed to build")
    image = json.loads(subprocess.check_output(["docker", "image", "inspect", tag], timeout=30))[0][
        "Id"
    ]
    choices = [
        (name, difficulty, sample)
        for name, family in families.items()
        for difficulty in options.difficulties
        if str(difficulty) in family["difficulty_dict"]
        for sample in range(options.samples_per_difficulty)
    ]
    random.Random(options.seed).shuffle(choices)
    candidates, rejected, seen = [], [], set()
    for index, (name, difficulty, sample) in enumerate(choices[: options.max_candidates]):
        if len(candidates) >= options.target:
            break
        family = families[name]
        params = scale_parameters(family, difficulty)
        seed = options.seed + index * options.max_generator_attempts
        identity = {
            "family": name,
            "difficulty": difficulty,
            "sample": sample,
            "seed": seed,
            "source_sha256": config["source_sha256"],
        }
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
        directory = destination / "candidates" / key
        try:
            code = fix_newlines_in_python_strings(
                import_needed_module_for_python(family["generate_testcase"])
            )
            solution = None
            for attempt in range(options.max_generator_attempts):
                wrapper = (
                    code
                    + f"\nif __name__ == '__main__':\n    import random as _scaler_random\n    _scaler_random.seed({seed + attempt})\n    print(generate_testcase({params!r}))\n"
                )
                generated = execute(
                    image,
                    wrapper,
                    3,
                    "",
                    directory / f"generator-{attempt}",
                    options.execution_timeout_sec,
                )
                if generated["returncode"] or generated["oom"] or generated["output_truncated"]:
                    continue
                try:
                    stdin, detail = parse_testcase(generated["stdout"])
                except (SyntaxError, ValueError):
                    continue
                for reference_index, (reference, language) in enumerate(
                    zip(
                        family["solutions"]["solution"],
                        family["solutions"]["language"],
                        strict=True,
                    )
                ):
                    if language not in {2, 3}:
                        continue
                    output = execute(
                        image,
                        reference,
                        language,
                        stdin,
                        directory / f"reference-{attempt}-{reference_index}",
                        options.execution_timeout_sec,
                    )
                    if (
                        not output["returncode"]
                        and not output["oom"]
                        and not output["output_truncated"]
                        and output["stdout"].strip()
                    ):
                        solution = output
                        break
                if solution:
                    break
            if solution is None:
                raise ValueError("No native generator/reference pair completed successfully")
            instance_hash = hashlib.sha256(
                json.dumps(
                    {"family": name, "detail": detail, "stdin": stdin}, sort_keys=True
                ).encode()
            ).hexdigest()
            if instance_hash in seen:
                raise ValueError("Duplicate concrete problem instance")
            answer = solution["stdout"].strip()
            if family.get("output_type") == "string":
                answer = answer.split()[0]
            elif family.get("output_type") == "array":
                answer = str([float(value) for value in answer.split()])
            record = {
                **identity,
                "id": key,
                "actual_seed": seed + attempt,
                "scale_parameters": params,
                "instance_sha256": instance_hash,
                "instruction": instruction_for(family, detail),
                "reference_answer": "\\boxed{" + answer + "}",
                "reference_code_sha256": solution["code_sha256"],
                "runtime_image": image,
            }
            save_record(directory / "instance.json", record)
            candidates.append(record)
            seen.add(instance_hash)
        except (ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            rejected.append({"id": key, "reason": "family_execution", "detail": str(exc)[:1000]})
        save_record(
            destination / "generation.json",
            {"candidates": candidates, "rejected": rejected, "attempted": index + 1},
        )
    result = {
        "candidates": candidates,
        "rejected": rejected,
        "attempted": len(candidates) + len(rejected),
    }
    save_record(destination / "generation.json", result)
    return result


def main() -> None:
    if os.environ.get("REPO2RLENV_REMOTE_WORKER") != "1":
        raise RuntimeError("SCALER generator and reference execution is remote only")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = generate(json.loads(args.config.read_text()), args.output)
    console.json({"generated": len(result["candidates"]), "rejected": len(result["rejected"])})


if __name__ == "__main__":
    main()
