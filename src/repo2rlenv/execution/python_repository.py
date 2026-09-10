"""Remote Python repository bootstrap and fresh test execution shared by recipes."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import uuid
from dataclasses import asdict
from pathlib import Path

from repo2rlenv.bootstrap import ensure_bootstrap
from repo2rlenv.execution.job import container_labels
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.quality.test_results import parse_junit
from repo2rlenv.spec.input import AuthSpec, BootstrapSpec, LLMSpec, RepoSpec
from repo2rlenv.spec.recipe_options import PythonRepositoryProfile


def _run(argv: list[str], *, timeout: int = 600, check: bool = True):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(f"Remote worker command failed ({result.returncode}): {argv[0]}")
    return result


def test_image(
    image: str,
    options: PythonRepositoryProfile,
    output: Path,
    *,
    replacement: tuple[Path, str] | None = None,
    replacements: dict[str, Path] | None = None,
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
        changes = dict(replacements or {})
        if replacement is not None:
            changes[replacement[1]] = replacement[0]
        for relative, local in changes.items():
            from repo2rlenv.emitter.bundle import relative_asset_path

            relative_asset_path("environment/" + relative)
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


def bootstrap_snapshot(repo: RepoSpec, options: PythonRepositoryProfile, destination: Path):
    if os.environ.get("REPO2RLENV_REMOTE_WORKER") != "1":
        raise RuntimeError("Repository execution is remote only")
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
    return boot, base
