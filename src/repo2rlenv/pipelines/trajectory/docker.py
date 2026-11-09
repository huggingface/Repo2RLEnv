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
from collections.abc import Callable
from pathlib import Path

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
    process = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
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
