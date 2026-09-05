"""Digest-bound copies for read-only audits; never import or execute live task code.

Usage::

    with isolated_audit_copy(live_task, audit_root, expected_digest=digest) as audit:
        text = (audit.task / "tests/test_contract.py").read_text()

For a subprocess, use ``[sys.executable, '-I', '-B', '-c', stdlib_only_check]``
and ``env=audit_subprocess_env()``; pass ``audit.task`` as data to inspect.
Bytecode suppression is defense in depth, not a sandbox. Neither this module nor
that invocation authorizes importing/executing task, target, or verifier code.
The context verifies source and copy again on exit, even if the audit raises.
Changed files and incomplete copies are retained as evidence, never cleaned up.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MAX_AUDIT_FILES = 256
MAX_AUDIT_ENTRIES = 512
MAX_AUDIT_BYTES = 32 * 1024 * 1024


class AuditIntegrityError(RuntimeError):
    """The expected immutable source/copy could not be established or preserved."""


@dataclass(frozen=True)
class AuditCopy:
    task: Path
    receipt: Path
    expected_digest: str


def audit_subprocess_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Disable Python bytecode and incidental cwd/PYTHONPATH imports for audit children."""
    env = dict(os.environ if base is None else base)
    env.pop("PYTHONPATH", None)
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONSAFEPATH="1")
    return env


def _read_regular(path: Path, *, maximum: int, consume) -> tuple[int, int]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise AuditIntegrityError(f"Nonregular or linked audit file: {path}")
        total = 0
        while chunk := stream.read(min(1024 * 1024, maximum + 1 - total)):
            total += len(chunk)
            if total > maximum:
                raise AuditIntegrityError(f"Audit byte bound exceeded: {path}")
            consume(chunk)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_mode, before.st_nlink) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_mode,
        after.st_nlink,
    ) or total != before.st_size:
        raise AuditIntegrityError(f"File changed while auditing: {path}")
    return total, stat.S_IMODE(before.st_mode)


def _inventory(root: Path) -> dict:
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise AuditIntegrityError(f"Audit tree is not a regular directory: {root}")
    pending, files, directories = [root], [], []
    entries = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for child in children:
                entries += 1
                if entries > MAX_AUDIT_ENTRIES:
                    raise AuditIntegrityError("Audit entry bound exceeded")
                info = child.stat(follow_symlinks=False)
                path = Path(child.path)
                if stat.S_ISDIR(info.st_mode):
                    directories.append(path.relative_to(root).as_posix())
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    files.append(path)
                    if len(files) > MAX_AUDIT_FILES:
                        raise AuditIntegrityError("Audit file bound exceeded")
                else:
                    raise AuditIntegrityError(f"Nonregular or linked audit entry: {path}")
    digest, inventory, total = hashlib.sha256(), [], 0
    # Match artifacts.digest_task's Path ordering and relative-path/null encoding.
    for path in sorted(files):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode() + b"\0")
        file_digest = hashlib.sha256()

        def consume(chunk, file_digest=file_digest):
            digest.update(chunk)
            file_digest.update(chunk)

        size, mode = _read_regular(path, maximum=MAX_AUDIT_BYTES - total, consume=consume)
        digest.update(b"\0")
        total += size
        inventory.append(
            {"path": relative, "size_bytes": size, "sha256": file_digest.hexdigest(), "mode": mode}
        )
    return {
        "digest": digest.hexdigest(),
        "files": inventory,
        "directories": sorted(directories),
        "total_bytes": total,
    }


def _copy_tree(source: Path, target: Path, inventory: dict) -> None:
    target.mkdir()
    for relative in inventory["directories"]:
        (target / relative).mkdir(parents=True, exist_ok=True)
    for entry in inventory["files"]:
        path = target / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with path.open("xb") as stream:

            def consume(chunk, digest=digest):
                stream.write(chunk)
                digest.update(chunk)

            size, mode = _read_regular(
                source / entry["path"], maximum=entry["size_bytes"], consume=consume
            )
        if (
            size != entry["size_bytes"]
            or digest.hexdigest() != entry["sha256"]
            or mode != entry["mode"]
        ):
            raise AuditIntegrityError(f"Source changed during copy: {entry['path']}")
        path.chmod(mode)


def _save(path: Path, record: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(record, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _observed(root: Path) -> dict:
    try:
        return _inventory(root)
    except (OSError, AuditIntegrityError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@contextmanager
def isolated_audit_copy(
    source: Path, destination: Path, *, expected_digest: str
) -> Iterator[AuditCopy]:
    """Yield a verified copy in a NEW disjoint root, then check both trees on exit.

    destination contains task/ and provenance.json. Existing/overlapping roots,
    unexpected source digests, linked entries and oversized trees are rejected.
    This is a change-detection workflow for exclusively owned audit directories,
    not an OS sandbox or a lock against concurrent source writers.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise ValueError("Expected source digest must be an exact lowercase SHA-256")
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or destination.is_symlink():
        raise ValueError("Audit roots must not be symlinks")
    source, destination = source.resolve(strict=True), destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Audit source and destination must not overlap (ancestor or descendant)")
    if destination.exists():
        raise ValueError("Audit destination must be new; existing evidence is never overwritten")
    before = _inventory(source)
    if before["digest"] != expected_digest:
        raise AuditIntegrityError(
            f"Unexpected source digest: expected {expected_digest}, observed {before['digest']}"
        )
    destination.mkdir(parents=True)
    audit = AuditCopy(destination / "task", destination / "provenance.json", expected_digest)
    record = {
        "schema_version": 1,
        "status": "copying",
        "created_at": datetime.now(UTC).isoformat(),
        "source_path": str(source),
        "copy_path": str(audit.task),
        "expected_source_digest": expected_digest,
        "source_before": before,
        "audit_started": False,
        "subprocess_policy": "stdlib-only inspection; python -I -B; audit_subprocess_env(); no task imports/execution",
    }
    _save(audit.receipt, record)
    try:
        _copy_tree(source, audit.task, before)
        record["source_before_audit"] = _observed(source)
        record["copy_before_audit"] = _observed(audit.task)
        if record["source_before_audit"] != before or record["copy_before_audit"] != before:
            raise AuditIntegrityError("Source or copy changed before audit; see provenance receipt")
        record.update(status="auditing", audit_started=True)
        _save(audit.receipt, record)
        yield audit
        record["status"] = "completed"
    except BaseException as exc:
        record.update(status="audit_error", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        record["source_after"] = _observed(source)
        record["copy_after"] = _observed(audit.task)
        changed = [name for name in ("source", "copy") if record[name + "_after"] != before]
        if changed:
            record.update(status="integrity_error", changed_trees=changed)
        record["finished_at"] = datetime.now(UTC).isoformat()
        _save(audit.receipt, record)
        if changed:
            raise AuditIntegrityError(
                f"Audit integrity changed in {', '.join(changed)}; see {audit.receipt}"
            )
