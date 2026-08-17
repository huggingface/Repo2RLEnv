"""Tests for `pipelines.env_setup` — the pipeline itself.

Every test here runs with no Docker, no LLM, and no network: `_sandbox_factory`,
`distill_setup_recipe`, and `_dry_run_gates` are all monkeypatched to fakes,
and `_run_oracle_gate` is monkeypatched to a fixed float so the (real, harbor-
CLI-shelling) oracle gate never runs. `_FakeSandbox` and `_bootstrap` come
from `tests/_env_setup_fakes.py` (shared with `tests/test_setup_recipe.py`).
"""

from __future__ import annotations

import pytest


def _gen_input(repo="pallets/click", ref="HEAD", *, with_llm=True):
    """GenerationInput requires repo + pipeline + output (input.py:197-206)."""
    from repo2rlenv.spec.input import (
        GenerationInput,
        LLMSpec,
        OutputSpec,
        PipelineName,
        PipelineSpec,
        RepoSpec,
    )

    return GenerationInput(
        repo=RepoSpec(url=repo, ref=ref),
        pipeline=PipelineSpec(name=PipelineName.ENV_SETUP),
        output=OutputSpec(destination="local", org="test", dataset_name="env-setup-test"),
        llm=LLMSpec(provider="anthropic", model="claude-sonnet-4-6") if with_llm else None,
    )


def test_env_setup_rejects_local_source(tmp_path):
    """A file:// source cannot serve a `docker build` clone. The capability
    makes the cli.py pre-flight reject it; the __init__ check is the guard for
    non-CLI callers, which docs/reference/API.md shows exactly.
    """
    from repo2rlenv.pipelines.env_setup import EnvSetupPipeline
    from repo2rlenv.sources import Capability
    from repo2rlenv.spec.options import EnvSetupOptions

    assert Capability.REMOTE_CLONE in EnvSetupPipeline.required_capabilities
    with pytest.raises(ValueError, match=r"REMOTE_CLONE|local"):
        EnvSetupPipeline(_gen_input(repo=f"file://{tmp_path}"), EnvSetupOptions())


def test_env_setup_classvars():
    from repo2rlenv.pipelines.env_setup import EnvSetupPipeline
    from repo2rlenv.spec.input import PipelineName

    assert EnvSetupPipeline.name == PipelineName.ENV_SETUP
    assert EnvSetupPipeline.requires_bootstrap is True
    assert EnvSetupPipeline.experimental is True
    assert EnvSetupPipeline.supported_languages is None
    # reward_kinds is a metadata key, not a class attribute — no pipeline
    # declares it as one.
    assert not hasattr(EnvSetupPipeline, "reward_kinds")


def test_env_setup_is_registered():
    from repo2rlenv.pipelines import PIPELINES
    from repo2rlenv.pipelines.env_setup import EnvSetupPipeline
    from repo2rlenv.spec.input import PipelineName

    assert PIPELINES[PipelineName.ENV_SETUP] is EnvSetupPipeline
