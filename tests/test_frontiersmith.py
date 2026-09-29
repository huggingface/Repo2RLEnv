from __future__ import annotations

import json
import tomllib
from types import SimpleNamespace

import pytest

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.emitter.bundle import inspect_bundle
from repo2rlenv.pipelines.recipes.frontiersmith.author import author
from repo2rlenv.pipelines.recipes.frontiersmith.export import export_task
from repo2rlenv.pipelines.recipes.frontiersmith.grade import finite_reward
from repo2rlenv.pipelines.recipes.frontiersmith.models import (
    Design,
    Infrastructure,
    Program,
    Review,
)
from repo2rlenv.pipelines.recipes.frontiersmith.pipeline import behavioral_divergence
from repo2rlenv.spec.input import LLMSpec
from repo2rlenv.spec.options import parse_options


def test_discovery_and_bounded_options():
    from repo2rlenv.pipelines.recipes.catalog import IMPLEMENTATIONS, get_recipe

    assert get_recipe("frontiersmith").pipeline == "optimization_synth"
    assert "frontiersmith" in IMPLEMENTATIONS
    assert parse_options("optimization_synth", {}, recipe="frontiersmith").target == 10
    with pytest.raises(ValueError):
        parse_options(
            "optimization_synth", {"target": 10, "max_candidates": 2}, recipe="frontiersmith"
        )
    with pytest.raises(ValueError):
        Review(approved=True, issues=["Scorer ignores a public constraint"], rationale="Mismatch")


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -0.1, 1.1, "0.8"])
def test_invalid_rewards_are_infrastructure_failures(value):
    with pytest.raises(ValueError):
        finite_reward(value)


def test_continuous_rewards_and_behavioral_diversity():
    assert finite_reward(0.63) == 0.63
    assert behavioral_divergence([[0.1, 0.9], [0.9, 0.1]], 0.01) == 1
    assert behavioral_divergence([[0.5, 0.5], [0.5, 0.5]], 0.01) == 0
    with pytest.raises(ValueError):
        behavioral_divergence([[0.5], [0.1, 0.5]], 0.01)


def test_export_is_parseable_private_and_content_bound(tmp_path):
    from harbor.models.task.task import Task

    design = Design(
        title="Fixture task",
        mutation="objective",
        instruction="Public behavior. " * 20,
        objective="maximize",
        feasibility="valid indices",
        score_formula="value / bound",
        baseline_strategy="first item",
        why_open_ended="multiple strategies",
    )
    infra = Infrastructure(
        generator="def generate(seed):\n    # eight independent fixture instances\n    return [{'v': [1, 2]}] * 8\n",
        scorer="def score(instance, output):\n    # invalid fixture submissions have zero reward\n    return 0.0\n",
    )
    solution = Program(strategy="fixture", code="import json\nprint(json.dumps({'chosen': [1]}))\n")
    kwargs = dict(name="frontiersmith-fixture", org="test", seed=42, lineage={})
    path = export_task(design, infra, solution, tmp_path, **kwargs)
    Task(path)
    config = tomllib.loads((path / "task.toml").read_text())
    assert config["environment"]["network_mode"] == "no-network"
    assert config["agent"]["user"] == "solver"
    assert config["verifier"]["user"] == "root"
    assert config["metadata"]["repo2env"]["evaluation"]["status"] == "unverified"
    assert not (path / "environment/solution.py").exists()
    assert not (path / "environment/scorer.py").exists()
    assert inspect_bundle(path)["integrity_passed"]
    assert export_task(design, infra, solution, tmp_path, resume=True, **kwargs) == path
    (path / "tests/scorer.py").write_text("raise RuntimeError('changed')")
    with pytest.raises(ValueError, match="does not match"):
        export_task(design, infra, solution, tmp_path, resume=True, **kwargs)


def test_model_receipt_resume_never_redispatches(tmp_path, monkeypatch):
    calls = []
    artifact = Review(approved=True, issues=[], rationale="Consistent")
    usage = {"input_tokens": 100, "output_tokens": 100}

    class Client:
        def __init__(self, **kwargs):
            self.responses = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                status="completed",
                output_text=artifact.model_dump_json(),
                model_dump=lambda **kwargs: {"usage": usage},
            )

    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
    monkeypatch.setattr("openai.OpenAI", Client)
    ledger = BudgetLedger(tmp_path / "ledger.sqlite3", limit_usd=1)
    spec = LLMSpec(provider="openai", model="gpt-6-luna")
    kwargs = dict(
        prompt="Review task",
        payload={"task": "fixture"},
        path=tmp_path / "call.json",
        ledger=ledger,
        operation="review:one",
        max_tokens=2048,
    )
    assert author(spec, Review, resume=False, **kwargs) == artifact
    assert author(spec, Review, resume=True, **kwargs) == artifact
    assert len(calls) == 1
    assert ledger.status()["reserved_usd"] == "0.000000"
    with pytest.raises(ValueError, match="different request"):
        author(spec, Review, resume=True, **{**kwargs, "payload": {"task": "changed"}})


def test_transport_error_preserves_reservation(tmp_path, monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            self.responses = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def create(self, **kwargs):
            raise TimeoutError("lost response")

    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
    monkeypatch.setattr("openai.OpenAI", Client)
    ledger = BudgetLedger(tmp_path / "ledger.sqlite3", limit_usd=1)
    kwargs = dict(
        prompt="Review",
        payload={},
        path=tmp_path / "call.json",
        ledger=ledger,
        operation="one",
        max_tokens=2048,
    )
    spec = LLMSpec(provider="openai", model="gpt-6-luna")
    with pytest.raises(TimeoutError):
        author(spec, Review, resume=False, **kwargs)
    assert ledger.status()["operations"][0]["status"] == "uncertain"
    with pytest.raises(ValueError, match="Reconcile"):
        author(spec, Review, resume=True, **kwargs)
    assert json.loads((tmp_path / "call.json").read_text())["state"] == "dispatched"


def test_invalid_received_output_is_charged_and_not_redispatched(tmp_path, monkeypatch):
    from repo2rlenv.pipelines.recipes.frontiersmith.author import InvalidArtifact

    calls = []

    class Client:
        def __init__(self, **kwargs):
            self.responses = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                status="completed",
                output_text='{"approved": true}',
                model_dump=lambda **kwargs: {"usage": {"input_tokens": 100, "output_tokens": 100}},
            )

    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
    monkeypatch.setattr("openai.OpenAI", Client)
    ledger = BudgetLedger(tmp_path / "ledger.sqlite3", limit_usd=1)
    spec = LLMSpec(provider="openai", model="gpt-6-luna")
    kwargs = dict(
        prompt="Review",
        payload={},
        path=tmp_path / "call.json",
        ledger=ledger,
        operation="invalid",
        max_tokens=2048,
    )
    for resume in (False, True):
        with pytest.raises(InvalidArtifact):
            author(spec, Review, resume=resume, **kwargs)
    assert len(calls) == 1
    assert ledger.status()["operations"][0]["status"] == "settled"
    assert float(ledger.status()["accounted_usd"]) > 0
    assert json.loads((tmp_path / "call.json").read_text())["state"] == "invalid_output"
