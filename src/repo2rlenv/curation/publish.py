from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from repo2rlenv.curation.artifacts import digest_task
from repo2rlenv.curation.campaign import ADMISSION_VERSION


def _excluded(relative: Path, *, directory: bool) -> bool:
    if any(
        part in {"artifacts", ".git", ".venv", "__pycache__", "node_modules"}
        for part in relative.parts
    ):
        return True
    if relative.name.startswith(".env") or relative.suffix in {".lock", ".tmp"}:
        return True
    runtime = next((i for i, part in enumerate(relative.parts) if part.endswith("-runtime")), None)
    if runtime is None:
        return False
    tail = relative.parts[runtime + 1 :]
    if not tail:
        return False
    if directory:
        # Only the OpenCode service log is needed below its private config,
        # cache, database and home tree. Native sessions/events live above it.
        return not (len(tail) == 1 and tail[0].startswith("opencode-"))
    if len(tail) == 1:
        return relative.name in {
            "runner-config.json",
            "auth.json",
            "models.json",
            "settings.json",
        } or relative.suffix not in {".json", ".jsonl", ".log"}
    return not (len(tail) == 2 and tail[0].startswith("opencode-") and tail[1] == "server.log")


def _evidence_files(root: Path):
    """Prune exports/caches before traversal, then reject remaining links."""
    for directory, dirs, files in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in sorted(dirs):
            path = current / name
            relative = path.relative_to(root)
            if _excluded(relative, directory=True):
                dirs.remove(name)
            elif path.is_symlink():
                raise ValueError(f"Cannot publish linked artifact: {relative}")
        dirs.sort()
        for name in sorted(files):
            path = current / name
            relative = path.relative_to(root)
            if _excluded(relative, directory=False):
                continue
            if path.is_symlink():
                raise ValueError(f"Cannot publish linked artifact: {relative}")
            if not path.is_file():
                raise ValueError(f"Cannot publish non-regular evidence: {relative}")
            yield path, relative.as_posix()


def _validate_admissions(root: Path) -> None:
    campaign = root / "manifest.json"
    comparison = root / "comparison.json"
    if campaign.exists() == comparison.exists():
        raise ValueError("Evidence requires exactly one campaign or comparison manifest")
    manifest = json.loads((comparison if comparison.exists() else campaign).read_text())
    accepted = (
        [row for row in manifest["rows"] if row["status"] == "accepted"]
        if comparison.exists()
        else manifest["accepted"]
    )
    seen = set()
    for row in accepted:
        identity = row["id"]
        if (
            not isinstance(identity, str)
            or identity in {"", ".", ".."}
            or Path(identity).name != identity
        ):
            raise ValueError("Invalid accepted task identity")
        if row.get("admission_version") != ADMISSION_VERSION:
            raise ValueError(f"Accepted task needs admission revalidation: {identity}")
        parent = root / "tasks"
        if comparison.exists():
            if row.get("runtime") not in {"langgraph", "pi", "opencode"}:
                raise ValueError("Invalid accepted comparison runtime")
            parent /= row["runtime"]
        task = parent / identity
        if task in seen:
            raise ValueError(f"Duplicate accepted task: {identity}")
        seen.add(task)
        if (
            not task.is_dir()
            or task.is_symlink()
            or parent.is_symlink()
            or digest_task(task) != row.get("task_digest")
        ):
            raise ValueError(f"Accepted task missing or changed after review: {identity}")


@contextmanager
def evidence_snapshot(root: Path):
    """Freeze files before hashing/uploading; reject concurrent campaign writers."""
    from repo2rlenv.curation.campaign import campaign_lock

    root = root.resolve()
    with TemporaryDirectory(prefix="r2e-evidence-") as temporary:
        snapshot = Path(temporary)
        with campaign_lock(root):
            for p, relative in _evidence_files(root):
                target = snapshot / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, target)
        yield snapshot


def publish_evidence(root: Path, bucket: str) -> str:
    """Append a content-addressed evidence snapshot to a private HF bucket."""
    with evidence_snapshot(root.resolve()) as snapshot:
        return _publish_snapshot(snapshot, bucket)


def _publish_snapshot(root: Path, bucket: str) -> str:
    from huggingface_hub import HfApi

    root = root.resolve()
    _validate_admissions(root)
    # Full exports stay private to the local run. review-submissions.json holds
    # exactly the bounded changed text inspected by the independent reviewer.
    files = list(_evidence_files(root))
    checksums = {name: hashlib.sha256(p.read_bytes()).hexdigest() for p, name in files}
    prefix = hashlib.sha256(json.dumps(checksums, sort_keys=True).encode()).hexdigest()
    api = HfApi()
    api.create_bucket(bucket, private=True, exist_ok=True)
    if not api.bucket_info(bucket).private:
        raise ValueError("Evidence contains privileged oracle data; select a private bucket")
    for start in range(0, len(files), 100):
        api.batch_bucket_files(
            bucket, add=[(p, f"{prefix}/{name}") for p, name in files[start : start + 100]]
        )
    api.batch_bucket_files(
        bucket, add=[(json.dumps(checksums, indent=2).encode(), f"{prefix}/checksums.json")]
    )
    return f"https://huggingface.co/buckets/{bucket}/tree/{prefix}"
