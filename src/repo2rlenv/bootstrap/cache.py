"""Filesystem cache for bootstrap results.

Layout under cache_dir (defaults to ./envs):

  <cache_dir>/<owner>__<name>/<short_commit>/
    bootstrap.json         # BootstrapResult, serialized
    Dockerfile             # reconstructed from agent commands
    transcript.jsonl       # full agent trace
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path

from repo2rlenv.bootstrap.spec import BootstrapResult, LanguageHint

logger = logging.getLogger(__name__)


def cache_key(repo: str, ref: str, cache_dir: Path) -> Path:
    """Return the cache directory for a (repo, ref) pair."""
    owner, _, name = repo.partition("/")
    if not name:
        name = owner
        owner = "_"
    short = (ref or "head")[:12]
    return cache_dir / f"{owner}__{name}" / short


def save(result: BootstrapResult, cache_dir: Path) -> Path:
    """Write a BootstrapResult to its cache slot. Returns the dir."""
    slot = cache_key(result.repo, result.ref, cache_dir)
    slot.mkdir(parents=True, exist_ok=True)

    payload = asdict(result)
    # Pathlib + enum aren't JSON-serializable by default
    payload["language"] = result.language.value
    if result.transcript_path is not None:
        payload["transcript_path"] = str(result.transcript_path)
    (slot / "bootstrap.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if result.dockerfile_reconstruction:
        (slot / "Dockerfile").write_text(result.dockerfile_reconstruction, encoding="utf-8")

    return slot


def load(repo: str, ref: str, cache_dir: Path) -> BootstrapResult | None:
    """Return a cached BootstrapResult, or None if not present / unparseable."""
    slot = cache_key(repo, ref, cache_dir)
    f = slot / "bootstrap.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("cache load failed for %s: %s", f, exc)
        return None

    # Coerce LanguageHint back from string
    if isinstance(data.get("language"), str):
        try:
            data["language"] = LanguageHint(data["language"])
        except ValueError:
            data["language"] = LanguageHint.UNKNOWN

    if isinstance(data.get("transcript_path"), str):
        data["transcript_path"] = Path(data["transcript_path"])

    # Filter to known fields so future BootstrapResult additions don't break cached loads
    if is_dataclass(BootstrapResult):
        known = {f.name for f in fields(BootstrapResult)}
        data = {k: v for k, v in data.items() if k in known}
    return BootstrapResult(**data)
