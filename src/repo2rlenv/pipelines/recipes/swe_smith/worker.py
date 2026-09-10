"""Remote-only procedural generation, reusing Repo2RLEnv's bootstrap/cache.

Acknowledgment: SWE-smith (MIT), pinned in provenance.md. Owned single-site
mutation enumeration and strict JUnit identity comparison replace the upstream
profile registry and validator, while retaining mutation/test/issue generation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shlex
import shutil
import subprocess
import uuid
from dataclasses import asdict
from pathlib import Path

from repo2rlenv.bootstrap import ensure_bootstrap
from repo2rlenv.campaigns.events import EventJournal, ProgressEvent
from repo2rlenv.execution.job import container_labels
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.pipelines.recipes.swe_smith.mutations import generate_mutations
from repo2rlenv.pipelines.recipes.swe_smith.options import SWESmithOptions
from repo2rlenv.quality.test_results import execution_contrast, parse_junit
from repo2rlenv.spec.input import AuthSpec, BootstrapSpec, LLMSpec, RepoSpec
from repo2rlenv.ui import console


def _run(argv: list[str], *, timeout: int = 600, check: bool = True):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(f"Remote worker command failed ({result.returncode}): {argv[0]}")
    return result


def test_image(
    image: str,
    options: SWESmithOptions,
    output: Path,
    *,
    replacement: tuple[Path, str] | None = None,
):
    """Run each test suite from a clean image, with no network or host mounts."""
    output.mkdir(parents=True, exist_ok=False)
    name = "r2e-contrast-" + uuid.uuid4().hex
    command = [
        "python",
        "-m",
        "pytest",
        *options.test_paths,
        "-q",
        "--tb=short",
        "--junitxml=/tmp/results.xml",
    ]
    _run(
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
            "--workdir",
            "/workspace",
            image,
            *command,
        ]
    )
    try:
        if replacement is not None:
            local, relative = replacement
            _run(["docker", "cp", str(local), f"{name}:/workspace/{relative}"])
        result = _run(
            ["docker", "start", "-a", name], timeout=options.test_timeout_sec, check=False
        )
        (output / "stdout.txt").write_text(result.stdout)
        (output / "stderr.txt").write_text(result.stderr)
        state = json.loads(_run(["docker", "inspect", name]).stdout)[0]["State"]
        save_record(output / "state.json", state)
        if state.get("OOMKilled") or state.get("Running"):
            raise ValueError("Test container did not complete within its resource contract")
        _run(["docker", "cp", f"{name}:/tmp/results.xml", str(output / "results.xml")])
        parsed = parse_junit((output / "results.xml").read_text(), returncode=state["ExitCode"])
        save_record(
            output / "results.json", {"returncode": parsed.returncode, "statuses": parsed.statuses}
        )
        return parsed
    finally:
        _run(["docker", "rm", "-f", name], timeout=30, check=False)


def generate(repo: RepoSpec, options: SWESmithOptions, destination: Path) -> dict:
    if os.environ.get("REPO2RLENV_REMOTE_WORKER") != "1":
        raise RuntimeError("This entry point runs inside a remote generation worker only")
    destination.mkdir(parents=True, exist_ok=False)
    journal = EventJournal(destination / "events.jsonl")

    def event(stage, state, message, **metrics):
        journal.emit(
            ProgressEvent(
                recipe="swe_smith", stage=stage, state=state, message=message, metrics=metrics
            )
        )

    event(
        "bootstrap", "started", "Build the explicit repository profile using the existing bootstrap"
    )
    dockerfile = (
        f"FROM {options.base_image}\nWORKDIR /workspace\n"
        + (
            f"RUN python -m pip install --no-cache-dir {shlex.join(options.dependencies)}\n"
            if options.dependencies
            else ""
        )
        + "COPY . /workspace\n"
        f"RUN {options.install_command}\n"
        "RUN rm -rf /workspace/.git /root/.cache/pip\n"
        "ENV PYTHONDONTWRITEBYTECODE=1\n"
    )
    digest = hashlib.sha256(dockerfile.encode()).hexdigest()
    profile = Path("/work/bootstrap-profiles") / f"{digest}.Dockerfile"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(dockerfile)
    boot = ensure_bootstrap(
        repo,
        BootstrapSpec(
            user_dockerfile=profile, cache_dir=Path("/work/bootstrap-cache"), max_seconds=900
        ),
        LLMSpec(provider="none", model="explicit-dockerfile"),
        AuthSpec(use_gh_cli=False),
    )
    save_record(destination / "bootstrap.json", asdict(boot))
    base = destination / "base"
    container = _run(["docker", "create", *container_labels(), boot.image_digest]).stdout.strip()
    try:
        _run(["docker", "cp", f"{container}:/workspace", str(base)])
    finally:
        _run(["docker", "rm", container], check=False)
    # A snapshot may contain bytecode or generated build files. Keep them out of
    # task source archives and refuse links rather than dereferencing host paths.
    for path in sorted(base.rglob("*"), reverse=True):
        if path.is_symlink():
            raise ValueError(
                f"Repository snapshot has a symlink requiring explicit support: {path.relative_to(base)}"
            )
        if path.is_dir() and path.name in {".git", "__pycache__", ".pytest_cache", "build", "dist"}:
            shutil.rmtree(path)
    healthy = test_image(boot.image_digest, options, destination / "healthy")
    if healthy.returncode != 0 or not healthy.passed:
        raise ValueError("Healthy repository tests must pass before mutation")
    event(
        "bootstrap",
        "completed",
        "Fresh healthy test suite passed",
        healthy_tests=len(healthy.passed),
    )

    paths = []
    for relative in options.source_paths:
        path = base / relative
        paths.extend([path] if path.is_file() else sorted(path.rglob("*.py")))
    paths = sorted(set(paths))
    random.Random(options.seed).shuffle(paths)
    records, rejected, entity_counts = [], [], {}
    attempted = 0
    for path in paths:
        relative = path.relative_to(base).as_posix()
        original = path.read_text()
        candidates = generate_mutations(
            original, relative, seed=options.seed, limit=options.max_candidates
        )
        for candidate in candidates:
            key = (relative, candidate.entity)
            if entity_counts.get(key, 0) >= options.max_per_entity:
                continue
            if attempted >= options.max_candidates or len(records) >= options.target:
                break
            attempted += 1
            output = destination / "candidates" / candidate.id
            output.mkdir(parents=True)
            (output / "mutated.py").write_text(candidate.source)
            (output / "original.py").write_text(original)
            (output / "mutation.diff").write_text(candidate.patch)
            event(
                "contrast",
                "started",
                candidate.entity,
                attempted=attempted,
                execution_passed=len(records),
            )
            try:
                defective = test_image(
                    boot.image_digest,
                    options,
                    output / "defective",
                    replacement=(output / "mutated.py", relative),
                )
                contrast = execution_contrast(healthy, defective)
            except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                rejected.append(
                    {"id": candidate.id, "reason": type(exc).__name__, "detail": str(exc)}
                )
                event("contrast", "failed", str(exc))
                continue
            record = {
                "id": candidate.id,
                "source_file": relative,
                "entity": candidate.entity,
                "operator": candidate.operator,
                "line": candidate.line,
                "repo": repo.url,
                "ref": boot.ref,
                "image_digest": boot.image_digest,
                "contrast": contrast,
            }
            save_record(output / "candidate.json", record)
            records.append(record)
            entity_counts[key] = entity_counts.get(key, 0) + 1
            event(
                "contrast",
                "completed",
                candidate.entity,
                attempted=attempted,
                execution_passed=len(records),
            )
            save_record(
                destination / "generation.json",
                {"candidates": records, "rejected": rejected, "attempted": attempted},
            )
        if attempted >= options.max_candidates or len(records) >= options.target:
            break
    result = {"candidates": records, "rejected": rejected, "attempted": attempted}
    save_record(destination / "generation.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    data = json.loads(args.config.read_text())
    result = generate(
        RepoSpec.model_validate(data["repo"]),
        SWESmithOptions.model_validate(data["options"]),
        args.output,
    )
    console.json({"attempted": result["attempted"], "execution_passed": len(result["candidates"])})


if __name__ == "__main__":
    main()
