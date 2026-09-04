from __future__ import annotations

import hashlib
import json
from pathlib import Path

from repo2rlenv.curation.artifacts import digest_task


def publish_evidence(root: Path, bucket: str) -> str:
    """Append a content-addressed evidence snapshot to a private HF bucket."""
    from huggingface_hub import HfApi

    root = root.resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    for accepted in manifest["accepted"]:
        task = root / "tasks" / accepted["id"]
        if digest_task(task) != accepted["task_digest"]:
            raise ValueError(f"Accepted task changed after review: {accepted['id']}")
    files = []
    for p in sorted(root.rglob("*")):
        relative = p.relative_to(root)
        # Do not upload untrusted solver filesystem exports; traces and grading
        # evidence suffice. No credentials or runtime caches enter the snapshot.
        if any(part in {"artifacts", ".git", ".venv", "__pycache__"} for part in relative.parts):
            continue
        if p.is_symlink():
            raise ValueError(f"Cannot publish linked artifact: {relative}")
        if p.is_file() and not p.name.startswith(".env") and p.suffix not in {".lock", ".tmp"}:
            files.append((p, relative.as_posix()))
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
