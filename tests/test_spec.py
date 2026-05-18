"""Validate the input spec models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from repo2rlenv.spec.input import GenerationInput, PipelineName, RepoSpec
from repo2rlenv.spec.options import (
    PRDiffOptions,
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
    with pytest.raises(ValidationError):
        RepoSpec(url="not_a_repo")


def test_full_input_roundtrips():
    payload = {
        "spec_version": "0.1.0",
        "repo": {"url": "huggingface/trl", "access": "auto"},
        "pipeline": {"name": "pr_diff", "options": {"limit": 5}},
        "llm": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "output": {
            "destination": "./out",
            "org": "myorg",
            "dataset_name": "trl-r2e",
        },
    }
    g = GenerationInput.model_validate(payload)
    assert g.repo.owner_name == ("huggingface", "trl")
    assert g.pipeline.name == PipelineName.PR_DIFF


def test_options_strict_extra_forbidden():
    with pytest.raises(ValidationError):
        PRDiffOptions(limit=10, unknown_field=42)


def test_parse_options_dispatches_correctly():
    opts = parse_options("pr_diff", {"limit": 7, "skip_drafts": False})
    assert isinstance(opts, PRDiffOptions)
    assert opts.limit == 7
    assert opts.skip_drafts is False


def test_parse_options_unknown_pipeline():
    with pytest.raises(ValueError):
        parse_options("not_real", {})


def test_generation_input_llm_defaults_to_none():
    g = GenerationInput.model_validate(
        {
            "repo": {"url": "huggingface/trl"},
            "pipeline": {"name": "pr_diff"},
            "output": {"destination": "./out", "org": "myorg", "dataset_name": "trl-r2e"},
        }
    )
    assert g.llm is None


def test_synthesis_pipeline_raises_without_llm():
    from repo2rlenv.bootstrap.spec import BootstrapResult, LanguageHint
    from repo2rlenv.pipelines.mutation_bugs import MutationBugsPipeline
    from repo2rlenv.spec.options import MutationBugsOptions

    gen = GenerationInput.model_validate(
        {
            "repo": {"url": "pallets/click"},
            "pipeline": {"name": "mutation_bugs"},
            "output": {"destination": "./out", "org": "myorg", "dataset_name": "test"},
        }
    )
    fake_bootstrap = BootstrapResult(
        image_tag="test",
        image_digest="sha256:abc",
        language=LanguageHint.PYTHON,
        repo="pallets/click",
        ref="main",
        rebuild_cmds=[],
        test_cmds=[],
        smoke_passed=True,
        iterations=1,
        build_time_sec=0.0,
        llm_provider="none",
    )
    with pytest.raises(ValueError, match="mutation_bugs requires --llm"):
        MutationBugsPipeline(gen, MutationBugsOptions(), bootstrap=fake_bootstrap)
