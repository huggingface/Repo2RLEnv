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
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import tomli_w

_SLUG = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
_ROLES = frozenset({"environment", "solution", "tests"})
_HASH_FIELD = "bundle_hash"
_TRACKED_FIELD = "tracked_files"
_EXECUTABLE_FIELD = "executable_files"


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


def _tracked_files(files: dict[str, TaskFile]) -> list[str]:
    """Every role-scoped path this bundle originally declared, regardless of
    executable-ness. instruction.md/task.toml are handled separately and
    never appear here — see relative_asset_path."""
    return sorted(name for name in files if name != "instruction.md")


def _executable_files(files: dict[str, TaskFile]) -> list[str]:
    return sorted(name for name, asset in files.items() if asset.executable)


def _identity(configuration: dict, files: dict[str, TaskFile]) -> str:
    # TOML parsing normalizes the stored representation before hashing on either
    # side, so dictionary insertion order and serialization layout cannot matter.
    import tomllib

    normalized = tomllib.loads(tomli_w.dumps(configuration))
    repo2env = normalized.get("metadata", {}).get("repo2env", {})
    repo2env.pop(_HASH_FIELD, None)
    # Evaluation is an advisory overlay, not executable task content. Excluding
    # only this namespace preserves existing unlabeled bundle identities; every
    # other configuration field and every task file remains bound to the hash.
    repo2env.pop("evaluation", None)
    if _TRACKED_FIELD in repo2env:
        # tracked_files/executable_files are themselves part of `normalized`
        # (hashed below), so a per-file "mode" would be redundant for a file
        # they cover — and re-deriving it from a stat() on the read side is
        # exactly the lossy round-trip these fields replace (NTFS has no
        # POSIX execute bit for regular files at all). A file NOT in
        # tracked_files (added to the directory after emission by other
        # tooling, e.g. the quality loop) isn't covered by either field, so
        # its mode still needs to ride along in the hash exactly as before —
        # otherwise a real chmod tamper on that file would go undetected.
        tracked_set = set(repo2env.get(_TRACKED_FIELD, []))
        file_record = {}
        for name, asset in sorted(files.items()):
            entry: dict[str, Any] = {"sha256": hashlib.sha256(asset.content).hexdigest()}
            if name not in tracked_set:
                entry["mode"] = asset.mode
            file_record[name] = entry
    else:
        # Legacy bundle (emitted before tracked_files existed): keep the
        # exact original hash shape so already-published bundle identities
        # never change.
        file_record = {
            name: {"sha256": hashlib.sha256(asset.content).hexdigest(), "mode": asset.mode}
            for name, asset in sorted(files.items())
        }
    record = {"configuration": normalized, "files": file_record}
    raw = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def bundle_hash(bundle: TaskBundle) -> str:
    files = {"instruction.md": TaskFile.text(bundle.instruction), **bundle.files}
    configuration = bundle.configuration()
    configuration["metadata"]["repo2env"][_TRACKED_FIELD] = _tracked_files(files)
    configuration["metadata"]["repo2env"][_EXECUTABLE_FIELD] = _executable_files(files)
    return _identity(configuration, files)


def _validated_asset_path_set(raw: object, *, field: str) -> set[str]:
    """Validate a task.toml path-list field before trusting it.

    Each entry must be a well-formed, role-scoped asset path (the same shape
    `relative_asset_path` enforces for every other asset), and the list must
    not contain duplicates — either would indicate a hand-edited or corrupted
    manifest, not a real bundle emitted by `write_bundle`.
    """
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"{field} must be a list of strings")
    if len(set(raw)) != len(raw):
        raise ValueError(f"{field} contains duplicate entries")
    for entry in raw:
        relative_asset_path(entry)
    return set(raw)


def inspect_bundle(path: Path) -> dict[str, Any]:
    """Read an emitted bundle without executing any task code or following links."""
    import tomllib

    if path.is_symlink() or not path.is_dir():
        raise ValueError("Task directory must be a real directory")
    configuration = tomllib.loads((path / "task.toml").read_text(encoding="utf-8"))
    repo2env = configuration.get("metadata", {}).get("repo2env", {})
    raw_tracked = repo2env.get(_TRACKED_FIELD)
    if raw_tracked is None:
        tracked = executable = None
    else:
        tracked = _validated_asset_path_set(raw_tracked, field=_TRACKED_FIELD)
        executable = _validated_asset_path_set(
            repo2env.get(_EXECUTABLE_FIELD, []), field=_EXECUTABLE_FIELD
        )
        if not executable <= tracked:
            raise ValueError(f"{_EXECUTABLE_FIELD} contains a path outside {_TRACKED_FIELD}")

    files: dict[str, TaskFile] = {}
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise ValueError(f"Task contains a symlink: {item.relative_to(path)}")
        if not item.is_file():
            if item.is_dir():
                continue
            raise ValueError(f"Task contains a special file: {item.relative_to(path)}")
        relative = item.relative_to(path).as_posix()
        if relative not in {"instruction.md", "task.toml"}:
            relative_asset_path(relative)
        if tracked is not None and (
            relative in tracked or relative in {"instruction.md", "task.toml"}
        ):
            # A file this bundle originally declared (instruction.md and
            # task.toml are always non-executable by construction, never
            # listed in tracked_files, but get the same platform-safe
            # treatment): executable-intent comes from the (hashed,
            # tamper-evident) manifest, not a stat() call that can't answer
            # correctly on every platform.
            is_executable = relative in executable
            if sys.platform != "win32":
                # Real chmod-tamper detection, exactly as strict as the
                # legacy check below — NTFS can't persist this bit at all, so
                # there's nothing meaningful to cross-check on Windows; the
                # hash still covers executable-intent via the manifest above.
                mode = item.stat().st_mode & 0o7777
                expected = 0o755 if is_executable else 0o644
                if mode != expected:
                    raise ValueError(f"Unexpected task asset mode: {relative}: {oct(mode)}")
        else:
            # Legacy bundle, OR a file added to this directory after emission
            # by other tooling (e.g. the quality loop appending an evidence
            # artifact) that this manifest was never asked to track. Fall
            # back to the original stat()-derived check, unaffected by this
            # fix either way.
            mode = item.stat().st_mode & 0o7777
            if mode not in {0o644, 0o755}:
                raise ValueError(f"Unexpected task asset mode: {relative}: {oct(mode)}")
            is_executable = mode == 0o755
        if relative != "task.toml":
            files[relative] = TaskFile(item.read_bytes(), is_executable)
    claimed = repo2env.get(_HASH_FIELD)
    actual = _identity(configuration, files)
    return {"bundle_hash": actual, "claimed_hash": claimed, "integrity_passed": claimed == actual}


def write_bundle(bundle: TaskBundle, destination: Path, *, resume: bool = False) -> Path:
    """Publish a fully materialized task, refusing existing targets or collisions."""
    configuration = bundle.configuration()
    files = {
        "instruction.md": TaskFile.text(bundle.instruction),
        **bundle.files,
    }
    # Computed against the SAME configuration + files that get written below
    # (rather than via a second bundle_hash(bundle) call building its own
    # configuration) so what's hashed and what's on disk can never diverge.
    configuration["metadata"]["repo2env"][_TRACKED_FIELD] = _tracked_files(files)
    configuration["metadata"]["repo2env"][_EXECUTABLE_FIELD] = _executable_files(files)
    configuration["metadata"]["repo2env"][_HASH_FIELD] = _identity(configuration, files)
    from repo2rlenv.emitter.evaluation import generated_evaluation

    configuration["metadata"]["repo2env"]["evaluation"] = generated_evaluation(
        subject_bundle_hash=configuration["metadata"]["repo2env"][_HASH_FIELD]
    )
    files["task.toml"] = TaskFile.text(tomli_w.dumps(configuration))
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
