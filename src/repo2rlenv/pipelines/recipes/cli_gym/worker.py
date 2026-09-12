"""Remote healthy baseline, environment inversion, restoration and test evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import uuid
from pathlib import Path

from repo2rlenv.execution.job import container_labels
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.execution.python_repository import bootstrap_snapshot, test_image
from repo2rlenv.pipelines.recipes.cli_gym.models import Inversion
from repo2rlenv.quality.test_results import parse_junit
from repo2rlenv.spec.input import RepoSpec
from repo2rlenv.spec.recipe_options import EnvironmentRepairOptions

_CONTROL = "/opt/repo2rlenv-verifier/bin/python"


def run(argv, *, timeout=60, check=True):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=check)


def create(image: str) -> str:
    name = "cli-inversion-" + uuid.uuid4().hex
    run(
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
            "2g",
            "--pids-limit",
            "256",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--workdir",
            "/workspace",
            image,
            "sleep",
            "infinity",
        ]
    )
    run(["docker", "start", name])
    return name


def prepare(repo: RepoSpec, options: EnvironmentRepairOptions, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=False)
    boot, base = bootstrap_snapshot(repo, options, destination)
    healthy = test_image(boot.image_digest, options, destination / "healthy")
    if healthy.returncode or not healthy.passed:
        raise ValueError("CLI-Gym needs a nonempty, passing gold test suite")
    protected = {}
    for relative in [*options.source_paths, *options.test_paths]:
        root = base / relative
        for path in [root] if root.is_file() else sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
                protected[path.relative_to(base).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    container = create(boot.image_digest)
    try:
        run(
            [
                "docker",
                "exec",
                container,
                "python",
                "-m",
                "venv",
                "--copies",
                "/opt/repo2rlenv-verifier",
            ]
        )
        packages = run(
            ["docker", "exec", container, "python", "-m", "pip", "list", "--format=json"]
        ).stdout
        image = run(["docker", "commit", container]).stdout.strip()
    finally:
        run(["docker", "rm", "-f", container], check=False)
    rng = random.Random(options.seed)
    tests = sorted(healthy.passed)
    candidates = []
    for index in range(options.max_candidates):
        identity = {"repo": repo.url, "ref": boot.ref, "index": index, "seed": options.seed}
        candidates.append(
            {
                **identity,
                "id": hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[
                    :20
                ],
                "direction": options.directions[index % len(options.directions)],
                "sampled_tests": rng.sample(tests, min(50, len(tests))),
                "image_digest": image,
            }
        )
    save_record(
        destination / "environment.json",
        {
            "protected": protected,
            "required_tests": tests,
            "packages": json.loads(packages),
            "file_paths": sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file())[
                :800
            ],
        },
    )
    result = {"candidates": candidates, "rejected": [], "attempted": len(candidates)}
    save_record(destination / "generation.json", result)
    return result


def suite(image: str, options, output: Path, recovery: Path | None = None) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    container = create(image)
    try:
        if recovery is not None:
            run(["docker", "cp", str(recovery), f"{container}:/tmp/recovery.sh"])
            result = run(
                ["docker", "exec", container, "bash", "/tmp/recovery.sh"],
                timeout=options.test_timeout_sec,
                check=False,
            )
            (output / "recovery.log").write_text(result.stdout + result.stderr)
            if result.returncode:
                return {
                    "returncode": result.returncode,
                    "stage": "recovery",
                    "passed": [],
                    "log": (result.stdout + result.stderr)[-18000:],
                }
        result = run(
            [
                "docker",
                "exec",
                container,
                "python",
                "-m",
                "pytest",
                "tests",
                "-q",
                "--tb=short",
                "--junitxml=/tmp/results.xml",
            ],
            timeout=options.test_timeout_sec,
            check=False,
        )
        log = result.stdout + result.stderr
        (output / "tests.log").write_text(log)
        copied = run(
            ["docker", "cp", f"{container}:/tmp/results.xml", str(output / "results.xml")],
            check=False,
        )
        passed = []
        if copied.returncode == 0:
            try:
                parsed = parse_junit(
                    (output / "results.xml").read_text(), returncode=result.returncode
                )
                passed = sorted(parsed.passed)
            except ValueError:
                pass
        evidence = {"returncode": result.returncode, "passed": passed, "log": log[-18000:]}
        save_record(output / "result.json", evidence)
        return evidence
    finally:
        run(["docker", "rm", "-f", container], check=False)


def evaluate(config, options, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=False)
    inversion = Inversion.model_validate(config["inversion"])
    generation = Path(config["generation"])
    environment = json.loads((generation / "environment.json").read_text())
    destruction = destination / "destruction.sh"
    destruction.write_text(inversion.destruction_shell)
    recovery = destination / "recovery.sh"
    recovery.write_text(inversion.recovery_shell)
    container = create(config["candidate"]["image_digest"])
    try:
        run(["docker", "cp", str(destruction), f"{container}:/tmp/inversion.sh"])
        changed = run(
            ["docker", "exec", container, "bash", "/tmp/inversion.sh"],
            timeout=options.test_timeout_sec,
            check=False,
        )
        if changed.returncode:
            result = {
                "contrast": False,
                "stage": "inversion",
                "log": (changed.stdout + changed.stderr)[-18000:],
            }
        else:
            script = "import hashlib,json,pathlib,sys; expected=json.loads(sys.argv[1]); bad=[p for p,h in expected.items() if not (pathlib.Path('/workspace')/p).is_file() or hashlib.sha256((pathlib.Path('/workspace')/p).read_bytes()).hexdigest()!=h]; sys.stdout.write(json.dumps(bad))"
            checked = run(
                [
                    "docker",
                    "exec",
                    container,
                    _CONTROL,
                    "-I",
                    "-c",
                    script,
                    json.dumps(environment["protected"]),
                ],
                check=False,
            )
            if checked.returncode or json.loads(checked.stdout):
                result = {
                    "contrast": False,
                    "stage": "protected_files",
                    "log": "Inversion modified source/tests or broke the isolated control interpreter",
                }
            else:
                run(["docker", "exec", container, "rm", "-f", "/tmp/inversion.sh"])
                image = run(["docker", "commit", container]).stdout.strip()
                baseline = suite(image, options, destination / "baseline")
                reference = suite(image, options, destination / "reference", recovery)
                required = set(environment["required_tests"])
                result = {
                    "contrast": bool(required)
                    and not required <= set(baseline["passed"])
                    and reference["returncode"] == 0
                    and required <= set(reference["passed"]),
                    "baseline": baseline,
                    "reference": reference,
                    "image_digest": image,
                    "missing_baseline_tests": sorted(required - set(baseline["passed"])),
                }
    finally:
        run(["docker", "rm", "-f", container], check=False)
    save_record(destination / "evaluation.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    options = EnvironmentRepairOptions.model_validate(config["options"])
    if config.get("mode") == "evaluate":
        evaluate(config, options, args.output)
    else:
        prepare(RepoSpec.model_validate(config["repo"]), options, args.output)


if __name__ == "__main__":
    main()
