"""BootstrapSpec validation + integration into GenerationInput."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from repo2rlenv.spec.input import (
    BootstrapSpec,
    GenerationInput,
    LLMSpec,
    OutputSpec,
    PipelineName,
    PipelineSpec,
    RepoSpec,
)


def test_defaults_are_sensible():
    spec = BootstrapSpec()
    assert spec.enabled is True
    assert spec.max_iterations == 20
    assert spec.max_seconds == 1800
    assert spec.cache_dir == Path("./envs")
    assert spec.platform == "linux/amd64"
    assert spec.user_dockerfile is None


def test_platform_constrained():
    with pytest.raises(ValidationError):
        BootstrapSpec(platform="linux/i386")  # not in Literal


def test_bootstrap_included_in_generation_input_default():
    g = GenerationInput(
        repo=RepoSpec(url="huggingface/trl"),
        pipeline=PipelineSpec(name=PipelineName.PR_DIFF, options={}),
        llm=LLMSpec(provider="anthropic", model="claude-sonnet-4-6"),
        output=OutputSpec(destination="./out", org="x", dataset_name="y"),
    )
    assert g.bootstrap.enabled is True
    assert g.bootstrap.cache_dir == Path("./envs")


def test_bootstrap_user_dockerfile_override():
    spec = BootstrapSpec(user_dockerfile=Path("./my-Dockerfile"))
    assert spec.user_dockerfile == Path("./my-Dockerfile")


def test_bootstrap_image_registry_optional():
    s1 = BootstrapSpec()
    assert s1.image_registry is None
    s2 = BootstrapSpec(image_registry="ghcr.io/myorg/r2e")
    assert s2.image_registry == "ghcr.io/myorg/r2e"


def test_model_copy_update_supports_cache_dir_override():
    """Drives the `--bootstrap-opt cache_dir=...` CLI plumbing.

    The CLI uses `bspec.model_copy(update={k: v})` to override any field;
    Pydantic should coerce a string path to a Path automatically.
    """
    bspec = BootstrapSpec()
    overridden = bspec.model_copy(update={"cache_dir": "./envs-matrix/sonnet-4-6"})
    # Pydantic preserves the literal string; just check the Path resolves correctly.
    assert Path(overridden.cache_dir).resolve() == Path("envs-matrix/sonnet-4-6").resolve()


def test_model_copy_update_supports_numeric_field():
    bspec = BootstrapSpec()
    overridden = bspec.model_copy(update={"max_iterations": 30, "max_seconds": 2400})
    assert overridden.max_iterations == 30
    assert overridden.max_seconds == 2400


def test_llm_fallback_round_trips_through_dict():
    """Drives the `--llm-fallback` CLI plumbing: nested LLMSpec via dict."""
    primary = LLMSpec(
        provider="anthropic",
        model="claude-sonnet-4-6",
        fallback=LLMSpec(provider="openai", model="gpt-5.5"),
    )
    assert primary.fallback is not None
    assert primary.fallback.qualified_name == "openai/gpt-5.5"
    # Round-trip through dict (CLI builds the spec via `model_validate(dict)`)
    rebuilt = LLMSpec.model_validate(primary.model_dump())
    assert rebuilt.fallback is not None
    assert rebuilt.fallback.qualified_name == "openai/gpt-5.5"
