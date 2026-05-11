"""Validate the input spec models."""

from __future__ import annotations

import pytest

from repo2rlenv.spec.input import GenerationInput, PipelineName, RepoSpec
from repo2rlenv.spec.options import (
    PRMiningLiteOptions,
    PRMiningOptions,
    parse_options,
)


def test_repo_spec_normalizes_short_form():
    r = RepoSpec(url="huggingface/trl")
    assert r.url == "https://github.com/huggingface/trl"
    assert r.owner_name == ("huggingface", "trl")


def test_repo_spec_strips_dot_git():
    r = RepoSpec(url="https://github.com/huggingface/trl.git")
    assert r.url == "https://github.com/huggingface/trl"


def test_repo_spec_rejects_bare_word():
    with pytest.raises(Exception):
        RepoSpec(url="not_a_repo")


def test_full_input_roundtrips():
    payload = {
        "spec_version": "0.1.0",
        "repo": {"url": "huggingface/trl", "access": "auto"},
        "pipeline": {"name": "pr_mining_lite", "options": {"limit": 5}},
        "llm": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "output": {
            "destination": "./out",
            "org": "myorg",
            "dataset_name": "trl-r2e",
        },
    }
    g = GenerationInput.model_validate(payload)
    assert g.repo.owner_name == ("huggingface", "trl")
    assert g.pipeline.name == PipelineName.PR_MINING_LITE


def test_options_strict_extra_forbidden():
    with pytest.raises(Exception):
        PRMiningLiteOptions(limit=10, unknown_field=42)


def test_parse_options_dispatches_correctly():
    opts = parse_options("pr_mining_lite", {"limit": 7, "skip_drafts": False})
    assert isinstance(opts, PRMiningLiteOptions)
    assert opts.limit == 7
    assert opts.skip_drafts is False


def test_parse_options_unknown_pipeline():
    with pytest.raises(ValueError):
        parse_options("not_real", {})
