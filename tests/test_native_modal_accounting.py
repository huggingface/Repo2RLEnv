"""Native lifecycle accounting must retain uncertain provider outcomes."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.execution.harbor_modal import (
    MeteredModalEnvironment,
    NativeAccounting,
    compute_estimate,
)
from repo2rlenv.quality.loop.client import RunBudget


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
