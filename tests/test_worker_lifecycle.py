from __future__ import annotations

import json

import pytest

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.execution.base import CommandResult, WorkerSpec
from repo2rlenv.execution.lifecycle import provision_worker, stop_worker


class FakeWorker:
    id = "test-worker"

    def terminate(self):
        pass


def test_lost_creation_response_preserves_identity_and_reservation(tmp_path, monkeypatch):
    ledger = BudgetLedger(tmp_path / "budget.db", limit_usd="5")

    def lost_response(spec):
        raise TimeoutError("provider response lost")

    monkeypatch.setattr("repo2rlenv.execution.lifecycle.create_worker", lost_response)
    spec = WorkerSpec(name="recoverable")
    with pytest.raises(TimeoutError):
        provision_worker(spec, tmp_path / "workers", ledger, reserve_usd="2")
    receipt = json.loads((tmp_path / "workers/recoverable.json").read_text())
    assert receipt["state"] == "creation_uncertain"
    assert receipt["spec"]["name"] == "recoverable"
    assert ledger.status()["reserved_usd"] == "2.000000"
    with pytest.raises(FileExistsError):
        provision_worker(spec, tmp_path / "workers", ledger, reserve_usd="2")


def test_cleanup_does_not_invent_zero_cost(tmp_path, monkeypatch):
    worker = FakeWorker()
    monkeypatch.setattr("repo2rlenv.execution.lifecycle.create_worker", lambda spec: worker)
    monkeypatch.setattr("repo2rlenv.execution.lifecycle.connect_worker", lambda *args: worker)
    ledger = BudgetLedger(tmp_path / "budget.db", limit_usd="5")
    _, path = provision_worker(
        WorkerSpec(name="cleanup"), tmp_path / "workers", ledger, reserve_usd="2"
    )
    receipt = stop_worker(path, ledger)
    assert receipt["state"] == "terminated"
    assert ledger.status()["reserved_usd"] == "2.000000"
    assert ledger.status()["accounted_usd"] == "0.000000"
    ledger.settle(receipt["operation_id"], "0.50", evidence="recorded rate estimate")
    assert ledger.status()["remaining_usd"] == "4.500000"
    assert stop_worker(path, ledger) == receipt


def test_command_failure_does_not_leak_output():
    result = CommandResult(1, "credential: example-sensitive-value")
    from repo2rlenv.execution.base import RemoteCommandError

    with pytest.raises(RemoteCommandError) as caught:
        result.checked("bootstrap")
    assert "sensitive" not in str(caught.value)
    assert caught.value.result is result
