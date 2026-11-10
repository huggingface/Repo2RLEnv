"""The Docker operations the pipeline needs. Uses the caller's Docker context (set DOCKER_HOST if
the socket isn't the default).

Images are built for PLATFORM (linux/amd64 by default, what training sandboxes run) so the image
that passes the trials is the one that gets published. The `platform` pipeline option sets it;
`native` uses the host's architecture (faster on Apple silicon, but then validate before
publishing). The `container_memory` option sets CONTAINER_MEMORY.
"""

from __future__ import annotations

import io
import shlex
import subprocess
import tarfile
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath

PLATFORM = "linux/amd64"
CONTAINER_MEMORY = "4g"  # the most any task.toml asks for (memory_mb)


def platform_args() -> list[str]:
    return [] if PLATFORM == "native" else ["--platform", PLATFORM]


def remove_as_root(path: Path) -> None:
    """Delete a folder holding files a container wrote as root (on Linux the host user can't)."""
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{path.parent.resolve()}:/parent",
            "busybox:1.36",
            "rm",
            "-rf",
            f"/parent/{path.name}",
        ],
        capture_output=True,
        check=True,
    )


def build_image(tag: str, context_dir: Path) -> str | None:
    """Build an image with BuildKit; return None on success, else the last line of the error.

    BuildKit (`docker buildx`, bundled with Docker Desktop) is needed to build for a platform other
    than the host's: the classic builder silently produces a host-platform image or fails.
    """
    process = subprocess.run(
        [
            "docker",
            "buildx",
            "build",
            "--load",
            "-q",
            *platform_args(),
            "-t",
            tag,
            str(context_dir),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=1200,
    )
    if process.returncode == 0:
        return None
    return (process.stderr.strip().splitlines() or ["docker build failed"])[-1][:300]


def run_in_container(
    image: str,
    script: str,
    mounts: dict[Path, str] | None = None,
    timeout_seconds: int = 900,
    network: bool = False,
) -> tuple[int, str]:
    """Run a bash script in a fresh container (networking off unless asked); return (exit code, output)."""
    volume_args = []
    for host_path, container_path in (mounts or {}).items():
        volume_args += ["-v", f"{Path(host_path).resolve()}:{container_path}"]
    network_args = [] if network else ["--network", "none"]
    # A runaway test must fail its own trial, not exhaust the host (the out-of-memory killer then hits anything).
    memory_args = ["--memory", CONTAINER_MEMORY, "--memory-swap", CONTAINER_MEMORY]
    name = "r2e-trajectory-" + uuid.uuid4().hex
    try:
        process = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                name,
                *platform_args(),
                *network_args,
                *memory_args,
                *volume_args,
                image,
                "bash",
                "-c",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_seconds,
        )
        return process.returncode, process.stdout + process.stderr
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


def _submission_archive(source: Path, target: Path, root: str) -> None:
    """Copy bounded ordinary files only; never extract an agent archive on the host."""
    size = 0
    names = set()
    with tarfile.open(source) as incoming, tarfile.open(target, "w") as outgoing:
        for member in incoming:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != root:
                raise ValueError("Submission archive escapes its artifact root")
            if member.name in names or len(names) >= 50_000:
                raise ValueError("Submission archive has duplicate or excessive entries")
            names.add(member.name)
            size += member.size
            if size > 256 * 1024 * 1024:
                raise ValueError("Submission archive exceeds 256 MiB")
            # Dependencies and links are not a submission transport. The native
            # grader rebuilds dependencies from the fresh image after transfer.
            if not (member.isfile() or member.isdir()):
                continue
            member.uid = member.gid = 0
            member.uname = member.gname = "root"
            member.mode = 0o755 if member.isdir() or member.mode & 0o111 else 0o644
            member.pax_headers = {}
            outgoing.addfile(member, incoming.extractfile(member) if member.isfile() else None)


def run_separate_trial(
    image: str,
    task_dir: Path,
    setup: str,
    verify: str,
    artifacts: list[dict],
    mounts: dict[Path, str],
) -> tuple[int, str]:
    """Run a control with the same fresh-verifier boundary as the emitted Harbor task.

    The agent receives /solution only for the reference control, never /tests.
    Stop it before artifact capture. Tests are copied into a new container, not
    bind-mounted: the root grader changes their ownership and permissions.
    """
    names = ["r2e-trajectory-" + uuid.uuid4().hex for _ in range(2)]
    agent, verifier = names
    transfer = "/tmp/" + uuid.uuid4().hex
    sources = [PurePosixPath(artifact["source"]) for artifact in artifacts]
    if any(str(source) not in {"/workspace", "/workdir/answer.txt"} for source in sources):
        raise ValueError("Unsupported trajectory artifact")
    # Docker cp merges existing directories. Replace each destination explicitly
    # so a submitted deletion cannot silently resurrect a file from the image.
    restore = ""
    for source in sources:
        incoming = f"{transfer}/{source.name}"
        restore += f"rm -rf -- {source}\nif [ -e {incoming} ]; then mv -- {incoming} {source}; fi\n"

    def docker(*args, **kwargs):
        return subprocess.run(["docker", *args], timeout=kwargs.pop("timeout", 60), **kwargs)

    def create(name, command, volumes):
        args = []
        for source, target in volumes.items():
            args += ["-v", f"{source.resolve()}:{target}"]
        docker(
            "create",
            "--name",
            name,
            *platform_args(),
            "--network",
            "none",
            "--memory",
            CONTAINER_MEMORY,
            "--memory-swap",
            CONTAINER_MEMORY,
            "--cpus",
            "2",
            "--pids-limit",
            "512",
            *args,
            image,
            "bash",
            "-c",
            command,
            capture_output=True,
            check=True,
        )

    try:
        create(agent, setup, {task_dir / "solution": "/solution:ro", **mounts})
        result = docker("start", "-a", agent, capture_output=True, text=True, timeout=900)
        if result.returncode:
            return result.returncode, result.stdout + result.stderr
        docker("stop", agent, capture_output=True)
        create(verifier, "set -e\n" + restore + verify, {})
        docker("cp", str(task_dir / "tests"), f"{verifier}:/tests", capture_output=True, check=True)
        with tempfile.TemporaryDirectory(prefix="trajectory-artifacts-") as temporary:
            raw, safe = Path(temporary) / "raw.tar", Path(temporary) / "safe.tar"
            empty = Path(temporary) / "empty"
            empty.mkdir()
            docker("cp", str(empty), f"{verifier}:{transfer}", capture_output=True, check=True)
            for source in sources:
                with raw.open("wb") as output:
                    copied = docker(
                        "cp", f"{agent}:{source}", "-", stdout=output, stderr=subprocess.PIPE
                    )
                if copied.returncode:
                    # Missing answers are a valid no-op control; infrastructure
                    # errors still fail at verification rather than becoming a reward.
                    continue
                _submission_archive(raw, safe, source.name)
                with safe.open("rb") as payload:
                    docker(
                        "cp",
                        "-",
                        f"{verifier}:{transfer}",
                        stdin=payload,
                        capture_output=True,
                        check=True,
                    )
            result = docker("start", "-a", verifier, capture_output=True, text=True, timeout=1800)
            return result.returncode, result.stdout + result.stderr
    finally:
        for name in names:
            docker("rm", "-f", name, capture_output=True)


def read_files_from_image(
    image: str, directory: str, keep: Callable[[str], bool]
) -> dict[str, str]:
    """Text files under `directory` in the image (skipping dependency folders) whose relative path passes `keep`."""
    list_and_archive = (
        f"cd {shlex.quote(directory)} && find . \\( -name node_modules -o -name .venv -o -name venv "
        f"-o -name .git -o -name dist \\) -prune -o -type f -size -512k -print0 | tar --null -T - -cf -"
    )
    process = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            *platform_args(),
            "--network",
            "none",
            image,
            "bash",
            "-c",
            list_and_archive,
        ],
        capture_output=True,
        timeout=300,
    )
    files = {}
    if process.returncode != 0:
        return files
    with tarfile.open(fileobj=io.BytesIO(process.stdout)) as archive:
        for member in archive.getmembers():
            relative_path = member.name.removeprefix("./")
            if member.isfile() and keep(relative_path):
                try:
                    files[relative_path] = archive.extractfile(member).read().decode()
                except UnicodeDecodeError:
                    pass
    return files
