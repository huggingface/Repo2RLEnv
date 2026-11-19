"""Dispatch regressions: no credentials, model calls or sandbox work."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repo2rlenv.cli import main
from repo2rlenv.config import load_generation_input
from repo2rlenv.spec.options import parse_options
from repo2rlenv.tasksmith.models import Options, Panel

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch):
    monkeypatch.setattr("repo2rlenv.cli._load_dotenv_if_present", lambda: None)


@pytest.mark.parametrize("pipeline_flags", [[], ["--pipeline", "terminal_synth"]])
@pytest.mark.parametrize("target", [1, 100])
def test_options_override_config_without_requiring_pipeline(monkeypatch, pipeline_flags, target):
    captured = []
    monkeypatch.setattr(
        "repo2rlenv.pipelines.recipes.cli.run_recipe",
        lambda config, **kwargs: captured.append(config) or 0,
    )
    assert (
        main(
            [
                "generate",
                "--config",
                str(EXAMPLES / "owned-seta.yaml"),
                *pipeline_flags,
                "--pipeline-opt",
                f"target={target}",
            ]
        )
        == 0
    )
    assert captured[0].pipeline.options["target"] == target
    assert captured[0].pipeline.options["max_candidates"] == 30


def test_native_json_flag_fails_before_execution(tmp_path, capsys):
    assert (
        main(
            [
                "generate",
                "--repo",
                "example/project",
                "--pipeline",
                "pr_diff",
                "--out",
                str(tmp_path),
                "--json",
            ]
        )
        == 2
    )
    assert "Native generation has no JSON" in json.loads(capsys.readouterr().out)["message"]


def test_owned_config_combines_endpoint_options_and_resume(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "repo2rlenv.pipelines.recipes.cli.run_recipe",
        lambda config, **kwargs: captured.append(config) or 0,
    )
    assert (
        main(
            [
                "generate",
                "--config",
                str(EXAMPLES / "owned-seta.yaml"),
                "--llm",
                "hosted_vllm/example",
                "--llm-endpoint",
                "http://localhost:8000/v1",
                "--llm-key-env",
                "LOCAL_MODEL_KEY",
                "--pipeline-opt",
                "target=3",
                "--resume",
            ]
        )
        == 0
    )
    config = captured[0]
    assert config.llm.endpoint == "http://localhost:8000/v1"
    assert config.llm.api_key_env == "LOCAL_MODEL_KEY"
    assert config.pipeline.options["target"] == 3
    assert config.pipeline.options["max_candidates"] == 30
    assert config.execution.resume is True


@pytest.mark.parametrize("limit", ["0", "0.01", "999"])
def test_owned_spending_flag_rejected_before_dispatch(monkeypatch, capsys, limit):
    def no_dispatch(*args, **kwargs):
        pytest.fail("An unsupported spending flag must fail before execution")

    monkeypatch.setattr("repo2rlenv.pipelines.recipes.cli.run_recipe", no_dispatch)
    assert (
        main(
            [
                "generate",
                "--config",
                str(EXAMPLES / "owned-seta.yaml"),
                "--max-spend-usd",
                limit,
                "--json",
            ]
        )
        == 2
    )
    error = json.loads(capsys.readouterr().out)
    assert error["error"] == "ValueError"
    assert "campaign init" in error["message"]


def test_unknown_owned_option_rejected_before_constructor(monkeypatch, capsys):
    def no_constructor(*args, **kwargs):
        pytest.fail("Unknown options must fail before constructing a pipeline")

    monkeypatch.setattr(
        "repo2rlenv.pipelines.terminal_synth.TerminalSynthesisPipeline", no_constructor
    )
    assert (
        main(
            [
                "generate",
                "--config",
                str(EXAMPLES / "owned-seta.yaml"),
                "--pipeline-opt",
                "targte=1",
                "--json",
            ]
        )
        == 2
    )
    assert "targte" in json.loads(capsys.readouterr().out)["message"]


@pytest.mark.parametrize(
    "flags, expected",
    [([], 5.0), (["--max-spend-usd", "0"], None), (["--max-spend-usd", "2"], 2.0)],
)
def test_native_bootstrap_spending_defaults_are_preserved(monkeypatch, tmp_path, flags, expected):
    from repo2rlenv.bootstrap.runner import BootstrapError
    from repo2rlenv.pipelines import PIPELINES

    class BootstrapPipeline:
        requires_bootstrap = True
        experimental = False

    captured = []

    def stop_before_bootstrap(repo, spec, *args, **kwargs):
        captured.append(spec.max_llm_spend_usd)
        raise BootstrapError("Test boundary: no remote work")

    monkeypatch.setitem(PIPELINES, "pr_diff", BootstrapPipeline)
    monkeypatch.setattr("repo2rlenv.bootstrap.ensure_bootstrap", stop_before_bootstrap)
    assert (
        main(
            [
                "--no-ui",
                "generate",
                "--repo",
                "example/project",
                "--pipeline",
                "pr_diff",
                "--out",
                str(tmp_path),
                "--llm",
                "openai/test-model",
                *flags,
            ]
        )
        == 1
    )
    assert captured == [expected]


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("owned-*.yaml")), ids=lambda p: p.stem)
def test_owned_examples_validate_without_runtime_inputs(path):
    config = load_generation_input(path)
    parse_options(
        config.pipeline.name.value, config.pipeline.options, recipe=config.pipeline.recipe
    )


def test_pr_runtime_cli_defers_bootstrap_and_preserves_overrides(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from repo2rlenv.pipelines import PIPELINES
    from repo2rlenv.pipelines.base import PipelineResult

    captured = []
    phases = []
    base = "c" * 40

    @contextmanager
    def bootstrap_view(**kwargs):
        assert kwargs["ref"] == base
        yield SimpleNamespace(
            on_turn=lambda *args: None,
            on_thinking=lambda *args: None,
            on_executing=lambda *args: None,
            on_phase=lambda *args: None,
            set_outcome=lambda **kwargs: phases.append(kwargs),
        )

    def bootstrap(repo, spec, llm, auth, **kwargs):
        assert phases == []
        captured.append((repo, spec, kwargs))
        return SimpleNamespace(
            image_digest="sha256:test",
            image_tag="local/test",
            rebuild_cmds=[],
            test_cmds=["pytest"],
        )

    class Pipeline:
        requires_bootstrap = True
        bootstrap_per_candidate = True
        experimental = False

        def __init__(self, input, options, *, bootstrap, bootstrap_factory):
            assert captured == []  # No HEAD environment may be built before discovery.
            assert bootstrap is None
            self.input = input
            self.bootstrap_factory = bootstrap_factory

        def run(self, out_dir):
            assert self.input.repo.ref == "HEAD"
            spec = self.input.bootstrap.model_copy(update={"target_paths": ["apps/editor/src.js"]})
            self.bootstrap_factory(
                self.input.repo.model_copy(update={"ref": base}), bootstrap_spec=spec
            )
            assert captured[0][1] is spec
            assert captured[0][1].target_paths == ["apps/editor/src.js"]
            assert self.input.bootstrap.target_paths == []
            return PipelineResult(0, 0, 0, out_dir, {})

    monkeypatch.setitem(PIPELINES, "pr_runtime", Pipeline)
    monkeypatch.setattr("repo2rlenv.llm.check_provider", lambda *args: None)
    monkeypatch.setattr("repo2rlenv.bootstrap.ensure_bootstrap", bootstrap)
    monkeypatch.setattr("repo2rlenv.ui.views.bootstrap.bootstrap_view_or_plain", bootstrap_view)
    assert (
        main(
            [
                "--no-ui",
                "generate",
                "--repo",
                "example/project",
                "--pipeline",
                "pr_runtime",
                "--llm",
                "openai/test-model",
                "--out",
                str(tmp_path),
                "--force-bootstrap",
                "--language",
                "node",
                "--base-image",
                "node:24-slim",
                "--max-spend-usd",
                "2",
                "--bootstrap-opt",
                "platform=linux/arm64",
                "--bootstrap-opt",
                "max_iterations=7",
            ]
        )
        == 1
    )  # Native generation returns 1 when no tasks are emitted.
    repo, spec, kwargs = captured[0]
    assert repo.ref == base
    assert spec.languages_hint == ["node"]
    assert spec.base_image == "node:24-slim"
    assert spec.platform == "linux/arm64"
    assert spec.max_iterations == 7
    assert spec.max_llm_spend_usd == 2
    assert kwargs["force"] is True
    assert all(
        kwargs[name] is not None
        for name in (
            "on_turn",
            "on_thinking",
            "on_executing",
            "on_phase",
        )
    )
    assert phases[0]["success"] is True


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("tasksmith-*.json")), ids=lambda p: p.stem)
def test_tasksmith_examples_validate(path):
    schema = Options if "options" in path.stem else Panel
    schema.model_validate_json(path.read_text())
