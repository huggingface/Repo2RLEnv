"""Image-build cache aliases for explicitly pinned public Hub assets."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath


def install_aliases(assets: list[dict], cache: Path) -> None:
    """Verify canonical identity before creating cache-local relative links."""
    from huggingface_hub import HfApi, hf_hub_download
    from huggingface_hub.utils import validate_repo_id

    cache = cache.resolve(strict=True)
    manifest = json.loads((cache / "tasksmith-assets.json").read_text())
    canonical = {item["repo_id"]: item for item in manifest}
    if len(canonical) != len(manifest):
        raise ValueError("Canonical asset manifest contains duplicate repositories")
    occupied = {"models--" + name.replace("/", "--") for name in canonical}
    planned, records = [], []
    api = HfApi()
    for asset in assets:
        validate_repo_id(asset["repo_id"])
        entry = canonical[asset["repo_id"]]
        revision = asset["revision"]
        if (
            not re.fullmatch(r"[0-9a-f]{40}", revision)
            or entry["revision"] != revision
            or entry["offline_main"] != revision
        ):
            raise ValueError("Cache alias must use the canonical asset's pinned revision")
        folder = "models--" + asset["repo_id"].replace("/", "--")
        source = cache / folder
        if source.is_symlink() or not source.is_dir() or source.resolve().parent != cache:
            raise ValueError("Canonical asset cache must be a direct directory inside the cache")
        if (source / "refs/main").read_text() != revision:
            raise ValueError("Canonical offline main differs from the pinned revision")
        files = []
        for record in entry["files"]:
            name = PurePosixPath(record["filename"])
            if name.is_absolute() or ".." in name.parts or name.as_posix() != record["filename"]:
                raise ValueError("Asset manifest contains an unsafe filename")
            path = source / "snapshots" / revision / name
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(source) or not resolved.is_file():
                raise ValueError("Canonical asset file escapes its repository cache")
            with resolved.open("rb") as stream:
                checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            if resolved.stat().st_size != record["bytes"] or checksum != record["sha256"]:
                raise ValueError("Canonical asset bytes differ from their recorded manifest")
            files.append((record, resolved))
        if not files:
            raise ValueError("Cache alias requires declared, verified asset files")
        for alias in asset["cache_aliases"]:
            validate_repo_id(alias)
            name = "models--" + alias.replace("/", "--")
            if name in occupied:
                raise ValueError(
                    "Cache aliases must not collide with another alias or canonical repository"
                )
            occupied.add(name)
            target = cache / name
            if target.is_symlink():
                if os.readlink(target) != folder:
                    raise ValueError(
                        "Existing cache alias points to a different or non-relative target"
                    )
            elif target.exists():
                raise ValueError("Cache alias would overwrite an existing cache entry")
            info = api.model_info(alias, revision=revision, token=False, timeout=30)
            if info.id != asset["repo_id"] or info.sha != revision:
                raise ValueError(
                    "Hub alias does not resolve to the canonical repository and pinned revision"
                )
            planned.append((alias, target, folder, revision, files))
            records.append(
                {
                    "alias": alias,
                    "repo_id": asset["repo_id"],
                    "revision": revision,
                    "cache_folder": name,
                    "files": [record for record, _ in files],
                }
            )
    output = cache / "tasksmith-asset-aliases.json"
    evidence = {"schema_version": "1", "aliases": sorted(records, key=lambda item: item["alias"])}
    if output.exists() and json.loads(output.read_text()) != evidence:
        raise ValueError(
            "Existing cache alias manifest differs; preserve it and rebuild from canonical assets"
        )
    created = []
    try:
        # All identity and collision checks above precede filesystem mutations.
        for alias, target, folder, revision, files in planned:
            if not target.is_symlink():
                target.symlink_to(folder, target_is_directory=True)
                created.append(target)
            for record, expected in files:
                for lookup_revision in ("main", revision):
                    found = Path(
                        hf_hub_download(
                            repo_id=alias,
                            filename=record["filename"],
                            revision=lookup_revision,
                            cache_dir=cache,
                            local_files_only=True,
                            token=False,
                        )
                    )
                    if found.resolve(strict=True) != expected:
                        raise ValueError(
                            "Offline alias lookup did not return the pinned canonical file"
                        )
        descriptor, temporary = tempfile.mkstemp(prefix=".tasksmith-aliases-", dir=cache)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(json.dumps(evidence, indent=2) + "\n")
            os.replace(temporary, output)
        finally:
            Path(temporary).unlink(missing_ok=True)
    except BaseException:
        for target in reversed(created):
            target.unlink(missing_ok=True)
        raise
