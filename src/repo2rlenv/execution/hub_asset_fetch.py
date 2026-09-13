"""Standalone image-build program; never import target models or receive credentials."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def fetch_assets(assets: list[dict], cache: Path) -> None:
    from huggingface_hub import get_hf_file_metadata, hf_hub_download, hf_hub_url

    manifests = []
    for asset in assets:
        sizes = {}
        for filename in asset["filenames"]:
            metadata = get_hf_file_metadata(
                hf_hub_url(asset["repo_id"], filename, revision=asset["revision"]), token=False
            )
            if metadata.commit_hash != asset["revision"] or metadata.size is None:
                raise ValueError("Hub metadata must resolve to the pinned revision and known size")
            sizes[filename] = metadata.size
        if sum(sizes.values()) > asset["max_bytes"]:
            raise ValueError(f"Asset exceeds download allowance: {asset['repo_id']}")
        records = []
        for filename, size in sizes.items():
            path = Path(
                hf_hub_download(
                    repo_id=asset["repo_id"],
                    filename=filename,
                    revision=asset["revision"],
                    cache_dir=cache,
                    token=False,
                )
            )
            if path.stat().st_size != size:
                raise ValueError("Downloaded asset size differs from pinned metadata")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            records.append({"filename": filename, "bytes": size, "sha256": digest})
        # Upstream tests often omit revision. Their offline 'main' lookup must
        # resolve to this explicit immutable pin, never the current Hub branch.
        ref = cache / ("models--" + asset["repo_id"].replace("/", "--")) / "refs/main"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text(asset["revision"])
        manifests.append({**asset, "files": records, "offline_main": asset["revision"]})
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "tasksmith-assets.json").write_text(json.dumps(manifests, indent=2) + "\n")
