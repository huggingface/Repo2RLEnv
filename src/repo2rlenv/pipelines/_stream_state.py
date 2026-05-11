"""Watermark state for `pr_stream` continuous mining.

A JSON file per (repo) under `<cache_dir>/streams/`. Tracks the latest
PR `merged_at` we successfully emitted so a re-run picks up from there
instead of re-mining the whole window.

State file layout (`<cache_dir>/streams/<owner>__<name>.json`):

    {
      "repo": "owner/name",
      "last_merged_at": "2026-04-12T08:15:22Z",
      "emitted_pr_numbers": [3299, 3328, 3330, ...]
    }

Hand-editable. Delete the file to force a fresh re-mine from `cutoff_date`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StreamState:
    """Watermark + dedup set for a single pr_stream invocation."""

    repo: str  # "owner/name"
    last_merged_at: str | None = None  # ISO8601 of newest PR emitted
    emitted_pr_numbers: list[int] = field(default_factory=list)

    @property
    def file_slug(self) -> str:
        """Match the cache-dir convention: `<owner>__<name>.json`."""
        return self.repo.replace("/", "__") + ".json"


def _state_dir(cache_dir: Path) -> Path:
    return cache_dir / "streams"


def state_path(repo: str, cache_dir: Path) -> Path:
    """Return the on-disk path for this repo's stream state, even if missing."""
    return _state_dir(cache_dir) / (repo.replace("/", "__") + ".json")


def load(repo: str, cache_dir: Path) -> StreamState:
    """Return the existing state, or a fresh default if no file exists."""
    path = state_path(repo, cache_dir)
    if not path.exists():
        return StreamState(repo=repo)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("stream state at %s unreadable (%s); starting fresh", path, exc)
        return StreamState(repo=repo)
    # Tolerate forward-compatible fields by filtering to known ones
    return StreamState(
        repo=data.get("repo", repo),
        last_merged_at=data.get("last_merged_at"),
        emitted_pr_numbers=list(data.get("emitted_pr_numbers", [])),
    )


def save(state: StreamState, cache_dir: Path) -> Path:
    """Write state to disk, creating parent dirs as needed."""
    path = state_path(state.repo, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    return path


def advance_watermark(state: StreamState, merged_ats: list[str]) -> StreamState:
    """Return a new state with `last_merged_at` advanced to max(current, new).

    ISO8601 strings are lexicographically sortable when in UTC, which is
    what `gh pr list` returns. We don't parse — string max is correct.
    """
    candidates = [m for m in merged_ats if m]
    if state.last_merged_at:
        candidates.append(state.last_merged_at)
    new = max(candidates) if candidates else state.last_merged_at
    return StreamState(
        repo=state.repo,
        last_merged_at=new,
        emitted_pr_numbers=state.emitted_pr_numbers,
    )
