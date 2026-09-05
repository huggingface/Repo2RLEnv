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
from repo2rlenv.curation.models import CampaignConfig, Review, validate_review_scores


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


def _validate_admissions(root: Path, *, origin_root: Path | None = None) -> None:
    campaign = root / "manifest.json"
    comparison = root / "comparison.json"
    if campaign.exists() == comparison.exists():
        raise ValueError("Evidence requires exactly one campaign or comparison manifest")
    manifest = json.loads((comparison if comparison.exists() else campaign).read_text())
    if not isinstance(manifest, dict):
        raise ValueError("Evidence manifest must be a JSON object")
    config_path = root / "config.json"
    config = CampaignConfig.model_validate(
        json.loads(config_path.read_text()) if config_path.exists() else manifest.get("config", {})
    )
    if "config" in manifest and (
        CampaignConfig.model_validate(manifest["config"]).acceptance_policy
        != config.acceptance_policy
    ):
        raise ValueError("Evidence configuration acceptance policy mismatch")
    protocol_path = root / "protocol.json"
    if comparison.exists():
        if "accepted" in manifest or protocol_path.exists():
            raise ValueError("Ambiguous comparison/pilot evidence manifest")
        rows = manifest.get("rows")
    elif "rows" in manifest:
        if (
            "accepted" in manifest
            or not protocol_path.is_file()
            or protocol_path.is_symlink()
            or not config_path.is_file()
            or config_path.is_symlink()
        ):
            raise ValueError("Pilot rows require an unambiguous protocol and configuration")
        protocol = json.loads(protocol_path.read_text())
        if not isinstance(protocol, dict) or not isinstance(protocol.get("config"), dict):
            raise ValueError("Pilot protocol requires its frozen configuration")
        if CampaignConfig.model_validate(protocol["config"]) != config:
            raise ValueError("Pilot protocol configuration mismatch")
        if "config" in manifest and CampaignConfig.model_validate(manifest["config"]) != config:
            raise ValueError("Pilot manifest configuration mismatch")
        rows = manifest["rows"]
    else:
        if protocol_path.exists() or "accepted" not in manifest:
            raise ValueError("Ambiguous campaign/pilot evidence manifest")
        rows = manifest["accepted"]
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Evidence admission rows must be a list of objects")
    if "rows" in manifest:
        if any(not isinstance(row.get("status"), str) for row in rows):
            raise ValueError("Evidence admission rows require a status")
        accepted = [row for row in rows if row["status"] == "accepted"]
    else:
        accepted = rows
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
        if row.get("acceptance_policy", "legacy") != config.acceptance_policy:
            raise ValueError(f"Accepted task acceptance policy mismatch: {identity}")
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
        if config.acceptance_policy == "validity" or any(
            name in row for name in ("legacy_score", "validity_score", "intrinsic_difficulty_score")
        ):
            review_path = Path(row.get("review_path", ""))
            try:
                relative = (
                    review_path.relative_to(origin_root or root)
                    if review_path.is_absolute()
                    else review_path
                )
            except ValueError as exc:
                raise ValueError(
                    "Accepted score receipt review is outside the evidence root"
                ) from exc
            retained_review = root / relative
            if (
                ".." in relative.parts
                or not relative.parts
                or relative.parts[0] != "candidates"
                or not retained_review.is_file()
                or retained_review.is_symlink()
                or not retained_review.resolve().is_relative_to(root.resolve())
            ):
                raise ValueError("Accepted score receipt requires its retained review")
            validate_review_scores(
                row, Review.model_validate_json(retained_review.read_text()), config
            )


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
    root = root.resolve()
    with evidence_snapshot(root) as snapshot:
        return _publish_snapshot(snapshot, bucket, origin_root=root)


def _publish_snapshot(root: Path, bucket: str, *, origin_root: Path | None = None) -> str:
    from huggingface_hub import HfApi

    root = root.resolve()
    _validate_admissions(root, origin_root=origin_root)
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
