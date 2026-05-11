"""BootstrapSpec validation + integration into GenerationInput."""

from __future__ import annotations

from pathlib import Path

import pytest

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
    with pytest.raises(Exception):
        BootstrapSpec(platform="linux/i386")  # not in Literal


def test_bootstrap_included_in_generation_input_default():
    g = GenerationInput(
        repo=RepoSpec(url="huggingface/trl"),
        pipeline=PipelineSpec(name=PipelineName.PR_MINING_LITE, options={}),
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
