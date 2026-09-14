from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("harbor")

from repo2rlenv.campaigns.budget import BudgetExceeded, BudgetLedger
from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.execution import harbor
from repo2rlenv.quality.loop import client
from repo2rlenv.quality.loop.client import RunBudget
from repo2rlenv.spec.input import LLMSpec


@pytest.fixture
def trial(tmp_path, monkeypatch):
    task = write_bundle(
        TaskBundle(
            name="example",
            org="tests",
            instruction="Write an answer.\n",
            files={
                "environment/Dockerfile": TaskFile.text(
                    "FROM python:3.12-slim\nWORKDIR /workspace\n"
                ),
                "solution/solve.sh": TaskFile.text("#!/bin/sh\ntrue\n", executable=True),
                "tests/test.sh": TaskFile.text("#!/bin/sh\ntrue\n", executable=True),
            },
            metadata={"recipe": "test", "recipe_version": "1", "reward_kinds": ["test_execution"]},
        ),
        tmp_path / "tasks",
    )
    ledger = BudgetLedger(tmp_path / "budget.db", limit_usd="100")
    shared = RunBudget(ledger, "batch-", "5", shared=True)
    budget = RunBudget(RunBudget(shared, "batch-child-", "10"), "batch-child-quality-", "10")
    shared.reserve("batch-sibling-worker", "4", "worker")
    worker = Mock(id="worker-fixture")
    worker.exec.return_value = SimpleNamespace(checked=lambda label: None)
    monkeypatch.setattr(harbor, "resolve_llm_api_key", lambda *args: "test-credential")
    launch = Mock()
    monkeypatch.setattr(harbor, "launch_job", launch)
    return SimpleNamespace(
        task=task,
        output=tmp_path / "trial",
        ledger=ledger,
        budget=budget,
        worker=worker,
        launch=launch,
        model=LLMSpec(provider="anthropic", model="claude-sonnet-4-6"),
    )


def run(trial, **kwargs):
    return harbor.run_trial(
        trial.worker,
        trial.task,
        trial.output,
        trial_id="batch-child-quality-solver",
        agent="terminus-2",
        model=trial.model,
        ledger=trial.budget,
        reservation_usd="3",
        **kwargs,
    )


def test_denied_cpu_trial_has_definite_no_dispatch_receipt(trial):
    with pytest.raises(BudgetExceeded) as caught:
        run(trial)
    receipt = json.loads((trial.output / "trial.json").read_text())
    assert receipt["state"] == "reservation_failed"
    assert receipt["trial_dispatched"] is False
    assert receipt["exception_type"] == "BudgetExceeded"
    assert receipt["reason"] == str(caught.value)
    assert receipt["reservation_denials"][0]["scope"] == "batch-"
    assert "test-credential" not in json.dumps(receipt)
    trial.worker.exec.assert_not_called()
    trial.worker.upload.assert_not_called()
    trial.launch.assert_not_called()
    assert len(trial.ledger.status()["operations"]) == 1
    with pytest.raises(FileExistsError, match="Reconcile"):
        run(trial, resume=True)
    trial.launch.assert_not_called()


def test_cpu_trial_dispatches_once_after_settlement_and_resumes_without_replay(trial, monkeypatch):
    def settle(_):
        receipt = json.loads((trial.output / "trial.json").read_text())
        assert receipt["state"] == "reservation_waiting"
        assert receipt["trial_dispatched"] is False
        trial.worker.exec.assert_not_called()
        trial.launch.assert_not_called()
        trial.ledger.settle("batch-sibling-worker", "1", evidence="sibling usage")

    monkeypatch.setattr(client.time, "sleep", settle)
    monkeypatch.setattr(
        harbor, "observe_job", lambda *args, **kwargs: {"state": "completed", "returncode": 0}
    )

    def unpack(archive, output, *, root_name):
        job = output / root_name
        directory = job / "one-trial"
        directory.mkdir(parents=True)
        (directory / "result.json").write_text(
            json.dumps(
                {
                    "verifier_result": {"rewards": {"reward": 1}},
                    "agent_result": {"cost_usd": 0.5},
                }
            )
        )
        return job

    monkeypatch.setattr(harbor, "unpack_evidence", unpack)
    assert run(trial, reservation_wait_sec=2).reward == 1
    trial.launch.assert_called_once()
    receipt = json.loads((trial.output / "trial.json").read_text())
    assert receipt["state"] == "completed"
    assert receipt["trial_dispatched"] is True
    assert receipt["reservation_wait"]["polls"] == 1
    assert trial.ledger.status()["accounted_usd"] == "1.500000"
    assert trial.ledger.status()["reserved_usd"] == "0.000000"
    assert run(trial, reservation_wait_sec=2, resume=True).reward == 1
    trial.launch.assert_called_once()


def test_cpu_wait_deadline_retains_exact_final_denial_without_effect(trial, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(client.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        client.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    with pytest.raises(BudgetExceeded):
        run(trial, reservation_wait_sec=2)
    receipt = json.loads((trial.output / "trial.json").read_text())
    assert receipt["state"] == "reservation_failed"
    assert receipt["trial_dispatched"] is False
    assert receipt["reservation_wait"]["elapsed_sec"] == 2
    assert receipt["reservation_wait"]["polls"] == 2
    assert receipt["reservation_denials"][0]["reason"] == "reservation_pressure"
    trial.worker.exec.assert_not_called()
    trial.worker.upload.assert_not_called()
    trial.launch.assert_not_called()
    assert len(trial.ledger.status()["operations"]) == 1
