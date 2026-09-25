"""Bounded Stack v3 repository rows, with explicit optional GitHub hydration.

The train dataset has inline repository files. The full corpus is a bucket and
is deliberately not silently treated as the same source contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath

from repo2rlenv.emitter.bundle import relative_asset_path

DATASET = "HuggingFaceCode/stack-v3-train"
MAX_BYTES = 32 * 1024 * 1024


def read_manifest(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("Stack manifest must be a regular file below 32 MiB")
    value = json.loads(path.read_text())
    validate_manifest(value)
    return value


def validate_manifest(value: dict):
    if value.get("dataset") != DATASET or not re.fullmatch(
        r"[0-9a-f]{40}", value.get("dataset_revision", "")
    ):
        raise ValueError("Use a pinned stack-v3-train dataset revision")
    row = value["row"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", row["repo_path"]):
        raise ValueError("Stack repository identity must be owner/name")
    if not re.fullmatch(r"[0-9a-f]{40}", row["commit_id"]):
        raise ValueError("Stack source requires its original 40-character commit")
    records = row["files"]
    if not records or len(records) > 10000 or row["num_files"] != len(records):
        raise ValueError("Use one complete bounded repository row")
    paths, total = set(), 0
    for item in records:
        path = relative_asset_path("environment/" + item["file_path"]).relative_to("environment")
        if str(path).casefold() in paths or any(
            part in {".git", ".env", "__pycache__"} for part in path.parts
        ):
            raise ValueError("Stack row contains duplicate, colliding or forbidden paths")
        paths.add(str(path).casefold())
        content = item["content"].encode()
        if b"\0" in content or len(content) > 2 * 1024 * 1024:
            raise ValueError("Stack source files must be bounded UTF-8 text")
        if item.get("license_type") != "permissive" or not item.get("detected_licenses"):
            raise ValueError(
                "This distributable profile requires explicit permissive file licenses"
            )
        total += len(content)
    if total > MAX_BYTES:
        raise ValueError("Stack row exceeds its materialization allowance")
    if any(str(parent) in paths for name in paths for parent in PurePosixPath(name).parents):
        raise ValueError("Stack files cannot also be parent directories")


def provenance(value: dict, materialization: str) -> dict:
    validate_manifest(value)
    if materialization not in {"inline", "hydrated"}:
        raise ValueError("Stack materialization must be inline or hydrated")
    row = value["row"]
    return {
        "source_kind": "stack_v3_" + materialization,
        "dataset": DATASET,
        "dataset_revision": value["dataset_revision"],
        "repo_path": row["repo_path"],
        "commit_id": row["commit_id"],
        "repo_id": row.get("repo_id"),
        "files": [
            {
                "path": item["file_path"],
                "content_id": item["content_id"],
                "sha256": hashlib.sha256(item["content"].encode()).hexdigest(),
                "licenses": item["detected_licenses"],
            }
            for item in row["files"]
        ],
    }


def materialize(value: dict, destination: Path):
    validate_manifest(value)
    destination.mkdir(parents=True, exist_ok=False)
    for item in value["row"]["files"]:
        path = destination / item["file_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item["content"])
