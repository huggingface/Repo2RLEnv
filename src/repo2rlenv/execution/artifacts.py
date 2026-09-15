"""Bounded transfer of owned runtime code and remote generation evidence."""

from __future__ import annotations

import hashlib
import tarfile
import tempfile
import zipfile
from importlib.resources import files
from pathlib import Path, PurePosixPath

from repo2rlenv.execution.base import RemoteWorker


def check_runtime_wheel(wheel: Path) -> str:
    """Refuse a stale wheel: every installed owned source/asset must match it."""
    root = Path(str(files("repo2rlenv")))
    with zipfile.ZipFile(wheel) as archive:
        for path in root.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            name = "repo2rlenv/" + path.relative_to(root).as_posix()
            try:
                content = archive.read(name)
            except KeyError as exc:
                raise ValueError(f"Worker wheel is missing {name}; run uv build") from exc
            if content != path.read_bytes():
                raise ValueError(f"Worker wheel is stale at {name}; run uv build")
    return hashlib.sha256(wheel.read_bytes()).hexdigest()


def install_runtime(worker: RemoteWorker, wheel: Path, log_dir: Path) -> str:
    digest = check_runtime_wheel(wheel)
    remote_dir = "/work/runtime/" + digest
    worker.exec(["mkdir", "-p", remote_dir], timeout=30).checked("Runtime directory")
    remote = remote_dir + "/" + wheel.name
    ready = worker.exec(["test", "-f", remote_dir + "/installed"], timeout=30)
    if ready.returncode == 0:
        return digest
    worker.upload(wheel, remote)
    worker.exec(
        ["uv", "venv", "--python", "/opt/repo2rlenv/bin/python", remote_dir + "/venv"],
        timeout=60,
    ).checked("Create isolated runtime")
    result = worker.exec(
        [
            "uv",
            "pip",
            "install",
            "--python",
            runtime_python(digest),
            "--reinstall-package",
            "repo2rlenv",
            remote + "[mutation,harbor]",
        ],
        timeout=300,
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "install.stdout").write_text(result.stdout)
    (log_dir / "install.stderr").write_text(result.stderr)
    result.checked("Install the owned runtime")
    worker.exec(["touch", remote_dir + "/installed"], timeout=30).checked(
        "Record runtime readiness"
    )
    return digest


def runtime_python(digest: str) -> str:
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("Runtime identity must be a SHA-256 digest")
    return "/work/runtime/" + digest + "/venv/bin/python"


def unpack_evidence(
    archive: Path, destination: Path, *, root_name: str, max_bytes: int = 512 * 1024 * 1024
) -> Path:
    """Never execute downloaded code or follow archive links on the controller."""
    target = destination / root_name
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    with tarfile.open(archive, "r:gz") as stream:
        members = stream.getmembers()
        if len(members) > 50_000 or sum(member.size for member in members) > max_bytes:
            raise ValueError("Remote evidence exceeds the archive limits")
        names = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.parts[0] != root_name
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError("Remote archive has unsafe paths, links or special files")
            if str(path) in names:
                raise ValueError("Remote archive has duplicate paths")
            names.add(str(path))
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".evidence-", dir=destination) as temporary:
            stream.extractall(temporary, members=members, filter="data")
            (Path(temporary) / root_name).rename(target)
    return target
