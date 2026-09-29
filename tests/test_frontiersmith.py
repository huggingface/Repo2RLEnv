from __future__ import annotations

import json
import tomllib
from types import SimpleNamespace

import pytest

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.emitter.bundle import inspect_bundle
from repo2rlenv.pipelines.recipes.frontiersmith.author import author
from repo2rlenv.pipelines.recipes.frontiersmith.export import export_task
from repo2rlenv.pipelines.recipes.frontiersmith.grade import (
    finite_reward,
    generated_cases,
    grade_output,
)
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


def test_zero_reward_does_not_mean_infeasible_and_disagreement_fails():
    zero = SimpleNamespace(score=lambda instance, output: 0.0)
    positive = SimpleNamespace(score=lambda instance, output: 0.5)
    valid = SimpleNamespace(is_feasible=lambda instance, output: True)
    invalid = SimpleNamespace(is_feasible=lambda instance, output: False)
    assert grade_output(zero, valid, {}, {}, "completed") == (0.0, True)
    assert grade_output(zero, invalid, {}, {}, "completed") == (0.0, False)
    with pytest.raises(ValueError, match="infeasible"):
        grade_output(positive, invalid, {}, {}, "completed")
    with pytest.raises(ValueError, match="return bool"):
        grade_output(zero, SimpleNamespace(is_feasible=lambda *args: 1), {}, {}, "completed")
    scores = iter([0.5, 0.6])
    with pytest.raises(ValueError, match="not deterministic"):
        grade_output(SimpleNamespace(score=lambda *args: next(scores)), valid, {}, {}, "completed")


def test_generator_rejects_invalid_or_unstable_case_sets():
    valid = SimpleNamespace(generate=lambda seed: [{"value": seed}] * 8)
    assert generated_cases(valid, 43) == [{"value": 43}] * 8
    for cases in ([], [{}] * 17, [None] * 8, [{"value": float("nan")}] * 8):
        with pytest.raises(ValueError):
            generated_cases(SimpleNamespace(generate=lambda seed, cases=cases: cases), 42)
    values = iter([[{"value": 1}] * 8, [{"value": 2}] * 8])
    with pytest.raises(ValueError, match="not deterministic"):
        generated_cases(SimpleNamespace(generate=lambda seed: next(values)), 42)


def test_exhausted_artifact_repairs_do_not_abort_other_seeds(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from repo2rlenv.pipelines.recipes.frontiersmith import pipeline
    from repo2rlenv.pipelines.recipes.frontiersmith.author import InvalidArtifact
    from repo2rlenv.spec.input import GenerationInput

    campaign = tmp_path / "campaign"
    ledger = BudgetLedger(campaign / "budget.sqlite3", limit_usd=1)
    seeds = tmp_path / "seeds.json"
    seeds.write_text(
        json.dumps(
            [
                dict(
                    id=name,
                    title=name,
                    problem="Find the shortest route on a small directed weighted graph.",
                    source="test",
                    license="Apache-2.0",
                    family="routing",
                )
                for name in ("one", "two")
            ]
        )
    )
    worker = tmp_path / "worker.json"
    worker.write_text(
        json.dumps(
            {
                "state": "running",
                "ledger": str(ledger.path),
                "worker_id": "fixture",
                "started_at": datetime.now(UTC).isoformat(),
                "spec": {"provider": "daytona", "timeout_sec": 3600},
            }
        )
    )
    spec = GenerationInput.model_validate(
        {
            "source": {"kind": "seeds", "path": str(seeds)},
            "pipeline": {"name": "optimization_synth", "recipe": "frontiersmith"},
            "llm": {"provider": "openai", "model": "gpt-6-sol"},
            "execution": {
                "campaign_dir": str(campaign),
                "run_id": "fixture",
                "worker_receipt": str(worker),
                "runtime_wheel": str(tmp_path / "fixture.whl"),
                "timeout_sec": 3600,
            },
            "output": {
                "destination": str(tmp_path / "tasks"),
                "org": "test",
                "dataset_name": "fixture",
            },
        }
    )
    calls = []

    def invalid(*args, **kwargs):
        calls.append(kwargs["operation"])
        raise InvalidArtifact("Received malformed model artifact")

    monkeypatch.setattr(pipeline, "check_runtime_wheel", lambda path: "a" * 64)
    monkeypatch.setattr(pipeline, "connect_worker", lambda *args: object())
    monkeypatch.setattr(pipeline, "prepare_docker", lambda worker: None)
    monkeypatch.setattr(pipeline, "install_runtime", lambda *args: "a" * 64)
    monkeypatch.setattr(pipeline, "author", invalid)
    options = parse_options(
        "optimization_synth", {"target": 1, "max_candidates": 2}, recipe="frontiersmith"
    )
    pipeline.FrontierSmithPipeline(spec, options).run(tmp_path / "tasks")
    record = json.loads((campaign / "runs/fixture/run.json").read_text())
    assert record["state"] == "completed"
    assert record["skipped"] == {"one": "invalid_model_artifact", "two": "invalid_model_artifact"}
    assert len(calls) == 6


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
        feasibility="def is_feasible(instance, output):\n    return output == {'chosen': [1]}\n",
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
    assert not (path / "environment/feasibility.py").exists()
    assert (path / "tests/feasibility.py").is_file()
    assert json.loads((path / "tests/contract.json").read_text())["explicit_feasibility"] is True
    instruction = (path / "instruction.md").read_text()
    assert "3 CPU seconds" in instruction and "64 KiB" in instruction
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
