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
    assert spec.cache_dir == Path("./workspace/bootstrap")
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
    assert g.bootstrap.cache_dir == Path("./workspace/bootstrap")


def test_bootstrap_user_dockerfile_override():
    spec = BootstrapSpec(user_dockerfile=Path("./my-Dockerfile"))
    assert spec.user_dockerfile == Path("./my-Dockerfile")


def test_bootstrap_image_registry_optional():
    s1 = BootstrapSpec()
    assert s1.image_registry is None
    s2 = BootstrapSpec(image_registry="ghcr.io/myorg/r2e")
    assert s2.image_registry == "ghcr.io/myorg/r2e"


def test_bootstrap_opt_converts_cache_dir_to_path():
    """`--bootstrap-opt cache_dir=...` must yield a Path, not the raw string (#245)."""
    from repo2rlenv.bootstrap.cache import cache_key
    from repo2rlenv.cli import _apply_bootstrap_opts

    overridden = _apply_bootstrap_opts(BootstrapSpec(), ["cache_dir=./envs-matrix/sonnet-4-6"])
    assert isinstance(overridden.cache_dir, Path)
    assert overridden.cache_dir == Path("envs-matrix/sonnet-4-6")
    # The bug surfaced here: `str / str` in the cache path computation.
    cache_key("pallets/click", "main", overridden.cache_dir)


def test_bootstrap_opt_converts_numeric_fields():
    from repo2rlenv.cli import _apply_bootstrap_opts

    overridden = _apply_bootstrap_opts(
        BootstrapSpec(), ["max_iterations=30", "max_seconds=2400", "max_llm_spend_usd=2.5"]
    )
    assert overridden.max_iterations == 30
    assert overridden.max_seconds == 2400
    assert overridden.max_llm_spend_usd == 2.5


def test_bootstrap_opt_keeps_unset_fields():
    from repo2rlenv.cli import _apply_bootstrap_opts

    bspec = BootstrapSpec(languages_hint=["python"], base_image="python:3.12-slim")
    overridden = _apply_bootstrap_opts(bspec, ["max_iterations=5"])
    assert overridden.languages_hint == ["python"]
    assert overridden.base_image == "python:3.12-slim"
    assert overridden.max_iterations == 5


@pytest.mark.parametrize(
    "item",
    ["no_such_field=1", "model_dump=1", "max_iterations=lots", "platform=linux/s390x"],
)
def test_bootstrap_opt_rejects_unknown_or_invalid_values(item):
    from repo2rlenv.cli import _apply_bootstrap_opts

    with pytest.raises(SystemExit):
        _apply_bootstrap_opts(BootstrapSpec(), [item])


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
