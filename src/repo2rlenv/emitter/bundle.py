"""General Harbor task bundles, with immutable file identities and atomic emission.

The existing patch-based HarborTask API remains in harbor.py. Recipes use this
representation when their reference is a program, setup script or other artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import tomli_w

_SLUG = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
_ROLES = frozenset({"environment", "solution", "tests"})
_HASH_FIELD = "bundle_hash"


def relative_asset_path(value: str) -> PurePosixPath:
    """Accept canonical POSIX paths only, independently of the controller OS."""
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or len(path.parts) < 2
        or path.parts[0] not in _ROLES
    ):
        raise ValueError(f"Invalid task asset path: {value!r}")
    return path


@dataclass(frozen=True)
class TaskFile:
    content: bytes
    executable: bool = False

    @classmethod
    def text(cls, content: str, *, executable: bool = False) -> TaskFile:
        return cls(content.encode("utf-8"), executable)

    @property
    def mode(self) -> int:
        return 0o755 if self.executable else 0o644


@dataclass
class TaskBundle:
    name: str
    org: str
    instruction: str
    files: dict[str, TaskFile]
    metadata: dict[str, Any]
    environment: dict[str, Any] = field(default_factory=dict)
    agent: dict[str, Any] = field(default_factory=dict)
    verifier: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    agent_timeout_sec: int = 600
    verifier_timeout_sec: int = 120

    def configuration(self) -> dict[str, Any]:
        """Build the Harbor 1.3 contract without a patch-only oracle."""
        if any(not _SLUG.fullmatch(value) or ".." in value for value in (self.name, self.org)):
            raise ValueError("Task name and organization must be filesystem-safe slugs")
        if not self.instruction.strip():
            raise ValueError("A task needs a nonempty instruction")
        if self.agent_timeout_sec <= 0 or self.verifier_timeout_sec <= 0:
            raise ValueError("Task timeouts must be positive")
        paths = {relative_asset_path(path) for path in self.files}
        for path in paths:
            if any(parent in paths for parent in path.parents):
                raise ValueError(f"Task asset is both a file and directory: {path}")
        for required in ("solution/solve.sh", "tests/test.sh"):
            asset = self.files.get(required)
            if asset is None or not asset.content or not asset.executable:
                raise ValueError(f"Task requires a nonempty executable {required}")
        if not any(path.parts[0] == "environment" for path in paths):
            raise ValueError("Task requires an environment definition")
        if not self.metadata.get("recipe") or not self.metadata.get("recipe_version"):
            raise ValueError("Task requires recipe and recipe_version provenance")
        if not self.metadata.get("reward_kinds"):
            raise ValueError("Task must explicitly declare reward_kinds")
        metadata = dict(self.metadata)
        metadata.pop(_HASH_FIELD, None)
        return {
            "schema_version": "1.3",
            "task": {"name": f"{self.org}/{self.name}"},
            "metadata": {"repo2env": metadata},
            "environment": {
                "cpus": 1,
                "memory_mb": 2048,
                "build_timeout_sec": 600,
                "network_mode": "no-network",
                **self.environment,
            },
            "agent": {"timeout_sec": self.agent_timeout_sec, **self.agent},
            "verifier": {"timeout_sec": self.verifier_timeout_sec, **self.verifier},
            "artifacts": self.artifacts,
        }


def _identity(configuration: dict, files: dict[str, TaskFile]) -> str:
    # TOML parsing normalizes the stored representation before hashing on either
    # side, so dictionary insertion order and serialization layout cannot matter.
    import tomllib

    normalized = tomllib.loads(tomli_w.dumps(configuration))
    normalized.get("metadata", {}).get("repo2env", {}).pop(_HASH_FIELD, None)
    record = {
        "configuration": normalized,
        "files": {
            name: {
                "sha256": hashlib.sha256(asset.content).hexdigest(),
                "mode": asset.mode,
            }
            for name, asset in sorted(files.items())
        },
    }
    raw = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def bundle_hash(bundle: TaskBundle) -> str:
    files = {"instruction.md": TaskFile.text(bundle.instruction), **bundle.files}
    return _identity(bundle.configuration(), files)


def inspect_bundle(path: Path) -> dict[str, Any]:
    """Read an emitted bundle without executing any task code or following links."""
    import tomllib

    if path.is_symlink() or not path.is_dir():
        raise ValueError("Task directory must be a real directory")
    files: dict[str, TaskFile] = {}
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise ValueError(f"Task contains a symlink: {item.relative_to(path)}")
        if not item.is_file():
            if item.is_dir():
                continue
            raise ValueError(f"Task contains a special file: {item.relative_to(path)}")
        relative = item.relative_to(path).as_posix()
        mode = item.stat().st_mode & 0o7777
        if mode not in {0o644, 0o755}:
            raise ValueError(f"Unexpected task asset mode: {relative}: {oct(mode)}")
        if relative not in {"instruction.md", "task.toml"}:
            relative_asset_path(relative)
        if relative != "task.toml":
            files[relative] = TaskFile(item.read_bytes(), mode == 0o755)
    configuration = tomllib.loads((path / "task.toml").read_text())
    claimed = configuration.get("metadata", {}).get("repo2env", {}).get(_HASH_FIELD)
    actual = _identity(configuration, files)
    return {"bundle_hash": actual, "claimed_hash": claimed, "integrity_passed": claimed == actual}


def write_bundle(bundle: TaskBundle, destination: Path, *, resume: bool = False) -> Path:
    """Publish a fully materialized task, refusing existing targets or collisions."""
    configuration = bundle.configuration()
    configuration["metadata"]["repo2env"][_HASH_FIELD] = bundle_hash(bundle)
    files = {
        "instruction.md": TaskFile.text(bundle.instruction),
        "task.toml": TaskFile.text(tomli_w.dumps(configuration)),
        **bundle.files,
    }
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / bundle.name
    lock = destination / f".{bundle.name}.lock"
    # Exclusive claims also protect two controllers generating the same task.
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    temporary = None
    try:
        os.close(fd)
        if target.exists() or target.is_symlink():
            if resume:
                existing = inspect_bundle(target)
                if existing["integrity_passed"] and existing["bundle_hash"] == bundle_hash(bundle):
                    return target
                raise ValueError("Existing task does not match the bundle being resumed")
            raise FileExistsError(target)
        temporary = Path(tempfile.mkdtemp(prefix=f".{bundle.name}-", dir=destination))
        for relative, asset in files.items():
            path = temporary / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(asset.content)
            path.chmod(asset.mode)
        temporary.rename(target)
        temporary = None
        return target
    finally:
        if temporary is not None:
            shutil.rmtree(temporary)
        lock.unlink()
