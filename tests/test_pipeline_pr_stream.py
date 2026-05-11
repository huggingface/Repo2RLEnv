"""pr_stream — watermark state + filter composition.

The pipeline itself is just a wrapper around pr_runtime; the load-bearing
new logic is in _stream_state.py (watermark file) and PRStreamPipeline's
_choose_since (effective lower bound). We test those directly without
touching Docker.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from repo2rlenv.pipelines import _stream_state
from repo2rlenv.pipelines.pr_stream import (
    PRStreamPipeline,
    _date_from_iso,
    _max_iso,
)
from repo2rlenv.spec.options import PRStreamOptions

# -------------------------- watermark state -----------------------------------


def test_state_default_when_no_file(tmp_path: Path):
    s = _stream_state.load("foo/bar", tmp_path)
    assert s.repo == "foo/bar"
    assert s.last_merged_at is None
    assert s.emitted_pr_numbers == []


def test_state_roundtrip(tmp_path: Path):
    s = _stream_state.StreamState(
        repo="owner/repo",
        last_merged_at="2026-05-12T10:00:00Z",
        emitted_pr_numbers=[1, 2, 3],
    )
    path = _stream_state.save(s, tmp_path)
    assert path.exists()
    loaded = _stream_state.load("owner/repo", tmp_path)
    assert loaded.last_merged_at == s.last_merged_at
    assert loaded.emitted_pr_numbers == [1, 2, 3]


def test_state_advance_picks_max(tmp_path: Path):
    s = _stream_state.StreamState(repo="x/y", last_merged_at="2026-01-01T00:00:00Z")
    new = _stream_state.advance_watermark(
        s,
        merged_ats=["2026-03-15T12:00:00Z", "2026-02-01T00:00:00Z"],
    )
    assert new.last_merged_at == "2026-03-15T12:00:00Z"


def test_state_advance_keeps_existing_when_new_is_older(tmp_path: Path):
    s = _stream_state.StreamState(repo="x/y", last_merged_at="2026-05-01T00:00:00Z")
    new = _stream_state.advance_watermark(s, merged_ats=["2026-01-01T00:00:00Z"])
    assert new.last_merged_at == "2026-05-01T00:00:00Z"


def test_state_file_slug_format():
    s = _stream_state.StreamState(repo="huggingface/trl")
    assert s.file_slug == "huggingface__trl.json"


def test_state_tolerates_corrupt_file(tmp_path: Path):
    """Bad JSON in the state file shouldn't crash a re-run — start fresh instead."""
    path = _stream_state.state_path("foo/bar", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not even close to json {{", encoding="utf-8")
    s = _stream_state.load("foo/bar", tmp_path)
    assert s.last_merged_at is None


# -------------------------- _choose_since composition -------------------------


def _make_pipeline(**opts) -> PRStreamPipeline:
    """Build a PRStreamPipeline without going through cmd_generate.

    Only used to test _choose_since — we don't call run(), so the bootstrap
    requirement doesn't matter."""
    from unittest.mock import MagicMock

    pipe = PRStreamPipeline.__new__(PRStreamPipeline)
    pipe.options = PRStreamOptions(**opts)
    pipe.bootstrap = MagicMock()
    pipe.input = MagicMock()
    pipe._progress_cb = None
    return pipe


def test_choose_since_no_watermark_no_cutoff_no_user():
    p = _make_pipeline()
    assert p._choose_since(watermark=None, cutoff=None, user_since=None) is None


def test_choose_since_picks_watermark_when_only_one():
    p = _make_pipeline()
    out = p._choose_since(watermark=date(2026, 3, 1), cutoff=None, user_since=None)
    assert out == date(2026, 3, 1)


def test_choose_since_picks_max_of_all_three():
    """Watermark wins when newer than cutoff; cutoff wins when newer than watermark."""
    p = _make_pipeline()
    out = p._choose_since(
        watermark=date(2026, 3, 1),
        cutoff=date(2026, 1, 1),
        user_since=date(2026, 2, 1),
    )
    assert out == date(2026, 3, 1)
    out = p._choose_since(
        watermark=date(2026, 1, 1),
        cutoff=date(2026, 4, 1),
        user_since=None,
    )
    assert out == date(2026, 4, 1)


def test_choose_since_user_since_can_override_if_newer():
    """If a user passes an explicit --since later than the watermark, respect it."""
    p = _make_pipeline()
    out = p._choose_since(
        watermark=date(2026, 1, 1),
        cutoff=date(2025, 12, 1),
        user_since=date(2026, 5, 1),
    )
    assert out == date(2026, 5, 1)


# -------------------------- iso helpers ---------------------------------------


def test_max_iso_handles_none():
    assert _max_iso(None, None) is None
    assert _max_iso("2026-01-01T00:00:00Z", None) == "2026-01-01T00:00:00Z"
    assert _max_iso(None, "2026-01-01T00:00:00Z") == "2026-01-01T00:00:00Z"


def test_max_iso_picks_later():
    assert _max_iso("2026-01-01T00:00:00Z", "2026-05-12T10:00:00Z") == "2026-05-12T10:00:00Z"


def test_date_from_iso():
    assert _date_from_iso("2026-04-12T08:15:22Z") == date(2026, 4, 12)
    assert _date_from_iso(None) is None
    assert _date_from_iso("not a date") is None


# -------------------------- pipeline contract ---------------------------------


def test_pr_stream_requires_bootstrap_attr():
    assert PRStreamPipeline.requires_bootstrap is True


def test_pr_stream_rejects_missing_bootstrap():
    from repo2rlenv.spec.input import (
        GenerationInput,
        LLMSpec,
        OutputSpec,
        PipelineName,
        PipelineSpec,
        RepoSpec,
    )

    gen_input = GenerationInput(
        repo=RepoSpec(url="huggingface/trl"),
        pipeline=PipelineSpec(name=PipelineName.PR_STREAM, options={}),
        llm=LLMSpec(provider="anthropic", model="claude-sonnet-4-6"),
        output=OutputSpec(destination="./out", org="x", dataset_name="y"),
    )
    with pytest.raises(RuntimeError, match="requires a BootstrapResult"):
        PRStreamPipeline(gen_input, PRStreamOptions(), bootstrap=None)
