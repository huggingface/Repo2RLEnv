"""Regression test: `generate` bootstrap CLI overrides must reach the pipeline.

`cmd_generate` builds an overridden `BootstrapSpec` from `--language`,
`--base-image`, `--max-spend-usd` and `--bootstrap-opt`. That spec used to be a
throwaway local only handed to `ensure_bootstrap()`, so a pipeline reading
`self.input.bootstrap` (e.g. `.platform`) still saw the un-overridden defaults.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.spec.input import GenerationInput


def _make_args(tmp_path: Path, **kwargs: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "config": None,
        "repo": str(tmp_path),
        "ref": "HEAD",
        "access": "auto",
        "pipeline": "commit_runtime",
        "pipeline_opt": ["limit=1"],
        "llm": "anthropic/claude-sonnet-4-6",
        "llm_fallback": None,
        "out": str(tmp_path / "out"),
        "org": None,
        "dataset_name": None,
        "visibility": "public",
        "max_spend_usd": 5.0,
        "language": None,
        "base_image": None,
        "force_bootstrap": False,
        "bootstrap_opt": None,
        "force_language": False,
        "no_ui": True,
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


class _StubPipeline:
    """Captures the GenerationInput the CLI hands to the pipeline."""

    requires_bootstrap = True
    seen: GenerationInput | None = None

    def __init__(self, input: GenerationInput, options: Any, bootstrap: Any = None) -> None:
        type(self).seen = input

    def run(self, out_dir: Path) -> PipelineResult:
        return PipelineResult(candidates=1, emitted=1, skipped=0, out_dir=out_dir, skip_reasons={})


@pytest.fixture
def captured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run cmd_generate with bootstrap + pipeline stubbed; yield a runner."""
    import repo2rlenv.bootstrap as bootstrap_mod
    from repo2rlenv.pipelines import PIPELINES

    monkeypatch.setitem(PIPELINES, "commit_runtime", _StubPipeline)
    monkeypatch.setattr(bootstrap_mod, "ensure_bootstrap", lambda *a, **kw: None, raising=True)

    def run(**kwargs: Any) -> GenerationInput:
        from repo2rlenv.cli import cmd_generate

        _StubPipeline.seen = None
        rc = cmd_generate(_make_args(tmp_path, **kwargs))
        assert rc == 0
        assert _StubPipeline.seen is not None
        return _StubPipeline.seen

    return run


class TestGenerateBootstrapOverrides:
    def test_language_override_visible_to_pipeline(self, captured) -> None:
        gen_input = captured(language="go")
        assert gen_input.bootstrap.languages_hint == ["go"]

    def test_base_image_override_visible_to_pipeline(self, captured) -> None:
        gen_input = captured(base_image="ubuntu:24.04")
        assert gen_input.bootstrap.base_image == "ubuntu:24.04"

    def test_max_spend_override_visible_to_pipeline(self, captured) -> None:
        gen_input = captured(max_spend_usd=12.5)
        assert gen_input.bootstrap.max_llm_spend_usd == 12.5

    def test_zero_max_spend_means_uncapped(self, captured) -> None:
        gen_input = captured(max_spend_usd=0.0)
        assert gen_input.bootstrap.max_llm_spend_usd is None

    def test_bootstrap_opt_overrides_visible_to_pipeline(self, captured) -> None:
        # platform is the field pipelines actually read off self.input.bootstrap
        gen_input = captured(bootstrap_opt=["platform=linux/arm64", "max_iterations=30"])
        assert gen_input.bootstrap.platform == "linux/arm64"
        assert gen_input.bootstrap.max_iterations == 30

    def test_ensure_bootstrap_and_pipeline_see_the_same_spec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured
    ) -> None:
        import repo2rlenv.bootstrap as bootstrap_mod

        seen: dict[str, Any] = {}

        def fake_ensure(repo: Any, bspec: Any, llm: Any, auth: Any, **kwargs: Any) -> None:
            seen["bspec"] = bspec
            return None

        monkeypatch.setattr(bootstrap_mod, "ensure_bootstrap", fake_ensure, raising=True)

        gen_input = captured(language="rust", bootstrap_opt=["platform=linux/arm64"])
        assert seen["bspec"] is gen_input.bootstrap
