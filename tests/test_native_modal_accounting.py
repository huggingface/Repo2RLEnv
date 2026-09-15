"""Native lifecycle accounting must retain uncertain provider outcomes."""

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("harbor")
pytest.importorskip("modal")

from repo2rlenv.campaigns.budget import BudgetExceeded, BudgetLedger
from repo2rlenv.execution.harbor_modal import (
    MeteredModalEnvironment,
    NativeAccounting,
    compute_estimate,
)
from repo2rlenv.quality.loop.client import RunBudget
from repo2rlenv.quality.loop.native import (
    model_cost_after_verifier_denial,
    model_was_not_dispatched,
    settle_completed_agent,
)


def test_untested_harbor_version_is_rejected_before_initialization(monkeypatch):
    monkeypatch.setattr("repo2rlenv.execution.harbor_modal.version", lambda _: "999.0.0")
    with pytest.raises(RuntimeError, match="tested Harbor"):
        MeteredModalEnvironment()


def test_model_hold_is_releasable_only_with_proven_pre_dispatch_denial(tmp_path):
    allocations = tmp_path / "allocations"
    allocations.mkdir()
    result = tmp_path / "result.json"
    data = {
        "agent_execution": None,
        "agent_result": None,
        "exception_info": {"exception_type": "BudgetExceeded"},
    }
    result.write_text(json.dumps(data))
    assert not model_was_not_dispatched(result, allocations)
    claim = allocations / "learner.json"
    claim.write_text(json.dumps({"state": "reservation_failed"}))
    assert model_was_not_dispatched(result, allocations)
    claim.write_text(json.dumps({"state": "creation_uncertain"}))
    assert not model_was_not_dispatched(result, allocations)
    claim.write_text(json.dumps({"state": "reservation_failed"}))
    result.write_text(json.dumps({**data, "agent_execution": {"started_at": "2026-09-13"}}))
    assert not model_was_not_dispatched(result, allocations)
    result.write_text(json.dumps({**data, "agent_result": {"cost_usd": 0.1}}))
    assert not model_was_not_dispatched(result, allocations)


def test_sandbox_pricing_and_cleanup_requirement():
    record = {
        "state": "terminated",
        "started_at": "2026-09-13T00:00:00+00:00",
        "stopped_at": "2026-09-13T00:01:40+00:00",
        "spec": {"cpus": 4, "memory_mb": 16384, "gpus": 2},
    }
    cost = compute_estimate(record)
    assert cost["allocation_rate_per_second"] == "0.00070840"
    assert cost["accounted_usd"] == "0.391680"
    with pytest.raises(ValueError, match="confirmed cleanup"):
        compute_estimate({**record, "state": "termination_uncertain"})


def test_completed_model_usage_survives_a_denied_verifier(tmp_path):
    allocations = tmp_path / "allocations"
    allocations.mkdir()
    claim = allocations / "verifier.json"
    claim.write_text(
        json.dumps({"state": "reservation_failed", "provider_name": "trial__verifier__task"})
    )
    (allocations / "learner.json").write_text(json.dumps({"state": "terminated"}))
    result = tmp_path / "result.json"
    data = {
        "agent_execution": {"finished_at": "2026-09-13T13:46:21Z"},
        "agent_result": {"cost_usd": 0.5612031},
        "exception_info": {"exception_type": "BudgetExceeded"},
    }
    result.write_text(json.dumps(data))
    assert str(model_cost_after_verifier_denial(result, allocations)) == "0.5612031"
    assert not model_was_not_dispatched(result, allocations)
    for cost in [None, True, -1, "NaN", "Infinity"]:
        result.write_text(json.dumps({**data, "agent_result": {"cost_usd": cost}}))
        assert model_cost_after_verifier_denial(result, allocations) is None
    result.write_text(json.dumps({**data, "agent_execution": {"finished_at": None}}))
    assert model_cost_after_verifier_denial(result, allocations) is None
    result.write_text(json.dumps(data))
    claim.write_text(json.dumps({"state": "reservation_failed", "provider_name": "trial__env"}))
    assert model_cost_after_verifier_denial(result, allocations) is None


def test_completed_agent_releases_unused_hold_before_verifier(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="5.00")
    ledger.reserve("trial:solver", "2.00", "solver")
    data = {
        "trial_name": "trial",
        "agent_execution": {"finished_at": "2026-09-13T14:11:07Z"},
        "agent_result": {"cost_usd": 0.1851843},
        "exception_info": None,
    }
    receipt = tmp_path / "completed-agent.json"
    assert settle_completed_agent(data, receipt, ledger, "trial:solver")
    ledger.reserve("verifier", "3.00", "separate verifier")
    assert ledger.status()["accounted_usd"] == "0.185185"
    assert json.loads(receipt.read_text())["verifier_pending"] is True


def test_unfinished_or_unmetered_agent_keeps_its_hold(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="5.00")
    ledger.reserve("trial:solver", "2.00", "solver")
    complete = {
        "agent_execution": {"finished_at": "2026-09-13T14:11:07Z"},
        "agent_result": {"cost_usd": 0.2},
        "exception_info": None,
    }
    candidates = [
        {**complete, "exception_info": {"exception_type": "AgentTimeoutError"}},
        {**complete, "agent_execution": None},
        *({**complete, "agent_result": {"cost_usd": cost}} for cost in [None, True, -1, "NaN"]),
    ]
    for data in candidates:
        assert not settle_completed_agent(data, tmp_path / "usage.json", ledger, "trial:solver")
    assert ledger.status()["reserved_usd"] == "2.000000"
    assert not (tmp_path / "usage.json").exists()


@pytest.mark.asyncio
async def test_lost_create_response_is_single_dispatch_and_retains_budget(monkeypatch, tmp_path):
    import modal

    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="10")
    accounting = NativeAccounting(RunBudget(ledger, "scale30-test", "10"), tmp_path / "receipts")
    env = object.__new__(MeteredModalEnvironment)
    env.accounting, env.receipt = accounting, tmp_path / "receipts/allocation.json"
    env.session_id, env._app_name, env._app, env._image = "unique", "test", object(), object()
    env.task_env_config = SimpleNamespace(cpus=2, memory_mb=4096, gpus=1)
    env.override_gpus, env._sandbox_timeout = None, 900
    monkeypatch.setattr(env, "_gpu_config", lambda: "L4:1")
    monkeypatch.setattr(env, "_sandbox_labels", lambda: {})
    calls = []

    async def create(*args, **kwargs):
        calls.append(kwargs)
        raise ConnectionError("Response lost")

    monkeypatch.setattr(modal.Sandbox, "create", SimpleNamespace(aio=create))
    with pytest.raises(ConnectionError):
        await env._create_sandbox()
    with pytest.raises(ValueError, match="already claimed"):
        await env._create_sandbox()
    assert len(calls) == 1 and calls[0]["block_network"] is True
    assert "secrets" not in calls[0] and "volumes" not in calls[0]
    assert ledger.status()["reserved_usd"] == "3.000000"
    assert ledger.status()["operations"][0]["status"] == "uncertain"


@pytest.mark.asyncio
async def test_denied_allocation_stays_budget_blocked_without_provider_dispatch(
    monkeypatch, tmp_path
):
    import modal

    from repo2rlenv.tasksmith.native_stages import NativeStages

    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="1")
    accounting = NativeAccounting(RunBudget(ledger, "denied-test", "10"), tmp_path / "allocations")
    env = object.__new__(MeteredModalEnvironment)
    env.accounting, env.receipt = accounting, tmp_path / "allocations/allocation.json"
    env.session_id, env._app_name = "denied", "test"
    env.task_env_config = SimpleNamespace(cpus=2, memory_mb=4096, gpus=1)
    env.override_gpus = None

    async def create(*args, **kwargs):
        pytest.fail("A denied reservation must never reach the provider")

    monkeypatch.setattr(modal.Sandbox, "create", SimpleNamespace(aio=create))
    with pytest.raises(BudgetExceeded) as denied:
        await env._create_sandbox()
    assert json.loads(env.receipt.read_text())["state"] == "reservation_failed"
    assert not ledger.status()["operations"]
    with pytest.raises(BudgetExceeded) as propagated:
        NativeStages.failure(tmp_path, denied.value)
    assert propagated.value is denied.value

    # A separate unknown allocation must still stop the controller for recovery.
    (accounting.directory / "unknown.json").write_text(json.dumps({"state": "creation_uncertain"}))
    with pytest.raises(RuntimeError, match="Reconcile native allocation"):
        NativeStages.failure(tmp_path, denied.value)


@pytest.mark.asyncio
async def test_cleanup_uncertainty_does_not_swallow_controller_cancellation(monkeypatch, tmp_path):
    from harbor.trial.trial import Trial

    from repo2rlenv.execution.harbor_modal import ACCOUNTING
    from repo2rlenv.quality.loop import native
    from repo2rlenv.quality.loop.models import LoopOptions

    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="10")
    budget = RunBudget(ledger, "cancel-test", "10")
    runner = native.NativeModalTrials(tmp_path, budget, LoopOptions())
    monkeypatch.setattr(native, "task_identity", lambda _: "sha256:fixture")

    async def stop():
        pass

    async def create(_):
        ACCOUNTING.get().environments.append(
            SimpleNamespace(record={"state": "creation_uncertain"}, stop=stop)
        )
        raise asyncio.CancelledError

    monkeypatch.setattr(Trial, "create", create)
    output = tmp_path / "trials/baseline"
    with pytest.raises(asyncio.CancelledError):
        await runner._run(tmp_path / "task", "baseline", "baseline", output)
    assert json.loads((output / "trial.json").read_text())["state"] == "cleanup_uncertain"
    assert ACCOUNTING.get() is None
