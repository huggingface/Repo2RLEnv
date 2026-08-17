"""Validate the input spec models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from repo2rlenv.spec.input import GenerationInput, PipelineName, RepoSpec
from repo2rlenv.spec.options import (
    EnvSetupOptions,
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


def test_env_setup_options_strict():
    with pytest.raises(ValidationError):
        EnvSetupOptions(unknown=1)


def test_env_setup_options_target_bounds():
    with pytest.raises(ValidationError):
        EnvSetupOptions(min_target_tests=0)
    with pytest.raises(ValidationError):
        EnvSetupOptions(max_target_tests=-1)
    # max_target_tests=0 means "whole suite" — must not raise.
    EnvSetupOptions(max_target_tests=0)
    # A cap below the floor is legal: "only repos whose suite passes >= 5
    # tests, graded on 3 of them" is a coherent, intentional configuration.
    EnvSetupOptions(min_target_tests=5, max_target_tests=3)


def test_env_setup_options_recipe_attempts_floor():
    # distill_setup_recipe's retry loop is `range(1, max_recipe_attempts + 1)`;
    # 0 or negative would skip the loop body and never attempt a recipe at all.
    with pytest.raises(ValidationError):
        EnvSetupOptions(max_recipe_attempts=0)
    with pytest.raises(ValidationError):
        EnvSetupOptions(max_recipe_attempts=-1)
    EnvSetupOptions(max_recipe_attempts=1)


def test_oracle_timeout_covers_inner_budgets():
    opts = EnvSetupOptions()
    assert opts.oracle_timeout_sec == 0
    expected = opts.max_setup_time_sec + opts.verifier_timeout_sec + opts.oracle_build_slack_sec
    assert opts.effective_oracle_timeout_sec == expected

    with pytest.raises(ValidationError):
        EnvSetupOptions(oracle_timeout_sec=expected - 1)

    # Exactly covering the sum is legal.
    EnvSetupOptions(oracle_timeout_sec=expected)


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
    from repo2rlenv.pipelines.code_instruct import CodeInstructPipeline
    from repo2rlenv.spec.options import CodeInstructOptions

    gen = GenerationInput.model_validate(
        {
            "repo": {"url": "pallets/click"},
            "pipeline": {"name": "code_instruct"},
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
    with pytest.raises(ValueError, match="code_instruct requires --llm"):
        CodeInstructPipeline(gen, CodeInstructOptions(), bootstrap=fake_bootstrap)
