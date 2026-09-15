from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from repo2rlenv.campaigns.budget import BudgetExceeded, BudgetLedger, OperationAlreadyRecorded
from repo2rlenv.quality.loop import client
from repo2rlenv.quality.loop.client import RunBudget
from repo2rlenv.quality.loop.models import LoopOptions


def budgets(tmp_path, *, global_cap="100", candidate_cap="10", quality_cap="10", shared=True):
    ledger = BudgetLedger(tmp_path / "budget.db", limit_usd=global_cap)
    batch = RunBudget(ledger, "batch-", "5", shared=shared)
    candidate = RunBudget(batch, "batch-child-", candidate_cap)
    quality = RunBudget(candidate, "batch-child-quality-", quality_cap)
    return ledger, batch, candidate, quality


def test_settlement_on_another_connection_unblocks_exactly_one_reservation(tmp_path):
    ledger, batch, _, quality = budgets(tmp_path)
    batch.reserve("batch-sibling-worker", "4", "worker")
    waiting = Event()

    def settle():
        assert waiting.wait(3)
        BudgetLedger(ledger.path).settle("batch-sibling-worker", "1", evidence="usage")

    with ThreadPoolExecutor(max_workers=1) as pool:
        settlement = pool.submit(settle)
        quality.reserve_with_wait(
            "batch-child-quality-solver",
            "3",
            "solver",
            timeout_sec=3,
            on_wait=lambda exc: waiting.set(),
        )
        settlement.result(timeout=3)
    status = ledger.status()
    assert status["accounted_usd"] == "1.000000"
    assert status["reserved_usd"] == "3.000000"
    with pytest.raises(OperationAlreadyRecorded):
        quality.reserve_with_wait("batch-child-quality-solver", "3", "retry", timeout_sec=3)
    assert len(status["operations"]) == 2


def test_deadline_does_not_release_sibling_hold_or_allocate(tmp_path, monkeypatch):
    ledger, batch, _, quality = budgets(tmp_path)
    batch.reserve("batch-sibling-worker", "4", "worker")
    clock = [0.0]
    monkeypatch.setattr(client.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        client.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    with pytest.raises(BudgetExceeded) as caught:
        quality.reserve_with_wait("batch-child-quality-solver", "3", "solver", timeout_sec=2)
    assert clock[0] == 2
    assert caught.value.denials[0].as_dict() == {
        "scope": "batch-",
        "reason": "reservation_pressure",
        "requested_usd": "3.000000",
        "limit_usd": "5.000000",
        "accounted_usd": "0.000000",
        "reserved_usd": "4.000000",
        "uncertain_usd": "0.000000",
        "remaining_usd": "1.000000",
    }
    assert len(ledger.status()["operations"]) == 1
    assert ledger.status()["reserved_usd"] == "4.000000"


@pytest.mark.parametrize(
    "kind",
    ["global", "candidate", "quality", "own_hold", "uncertain", "spent", "unmarked", "disabled"],
)
def test_nonrecoverable_or_disabled_limits_fail_without_sleep(tmp_path, monkeypatch, kind):
    ledger, batch, candidate, quality = budgets(
        tmp_path,
        global_cap="5" if kind == "global" else "100",
        candidate_cap="2" if kind == "candidate" else "10",
        quality_cap="2" if kind == "quality" else "10",
        shared=kind != "unmarked",
    )
    operation = "batch-child-worker" if kind == "own_hold" else "batch-sibling-worker"
    batch.reserve(operation, "4", "worker")
    if kind == "uncertain":
        ledger.mark_uncertain(operation, "uncertain effect")
    if kind == "spent":
        ledger.settle(operation, "4", evidence="usage")
    monkeypatch.setattr(client.time, "sleep", lambda _: pytest.fail("Must not wait on this denial"))
    with pytest.raises(BudgetExceeded) as caught:
        quality.reserve_with_wait(
            "batch-child-quality-solver",
            "3",
            "solver",
            timeout_sec=0 if kind == "disabled" else 2,
        )
    scopes = {denial.scope for denial in caught.value.denials}
    if kind == "candidate":
        assert scopes == {"batch-", candidate.prefix}
    if kind == "quality":
        assert scopes == {"batch-", quality.prefix}
    if kind == "global":
        assert scopes == {None, "batch-"}
    assert len(ledger.status()["operations"]) == 1


def test_same_operation_claimed_during_wait_is_never_replayed(tmp_path, monkeypatch):
    ledger, batch, _, quality = budgets(tmp_path)
    batch.reserve("batch-sibling-worker", "4", "worker")

    def contender(_):
        ledger.settle("batch-sibling-worker", "1", evidence="usage")
        quality.reserve("batch-child-quality-solver", "3", "other controller admitted")

    monkeypatch.setattr(client.time, "sleep", contender)
    with pytest.raises(OperationAlreadyRecorded):
        quality.reserve_with_wait("batch-child-quality-solver", "3", "solver", timeout_sec=2)
    assert ledger.status()["reserved_usd"] == "3.000000"
    assert len(ledger.status()["operations"]) == 2


def test_legacy_exception_is_not_interpreted_as_retryable(tmp_path, monkeypatch):
    _, _, _, quality = budgets(tmp_path)
    legacy = BudgetExceeded("legacy denial")

    def denied(*args):
        raise legacy

    monkeypatch.setattr(quality, "reserve", denied)
    monkeypatch.setattr(client.time, "sleep", lambda _: pytest.fail("No typed capacity evidence"))
    with pytest.raises(BudgetExceeded, match="legacy denial") as caught:
        quality.reserve_with_wait("batch-child-quality-solver", "3", "solver", timeout_sec=2)
    assert caught.value is legacy
    assert legacy.denials == ()


def test_ordinary_reserve_never_waits_even_for_a_shared_scope(tmp_path, monkeypatch):
    _, batch, _, quality = budgets(tmp_path)
    batch.reserve("batch-sibling-worker", "4", "worker")
    monkeypatch.setattr(
        client.time, "sleep", lambda _: pytest.fail("Only CPU solver opts into wait")
    )
    with pytest.raises(BudgetExceeded):
        quality.reserve("batch-child-quality-review", "3", "review model")


def test_insufficient_shared_limit_fails_without_wait(tmp_path, monkeypatch):
    ledger, _, _, quality = budgets(tmp_path)
    monkeypatch.setattr(client.time, "sleep", lambda _: pytest.fail("Request can never fit"))
    with pytest.raises(BudgetExceeded) as caught:
        quality.reserve_with_wait("batch-child-quality-solver", "6", "solver", timeout_sec=2)
    assert caught.value.denials[0].reason == "request_exceeds_limit"
    assert not ledger.status()["operations"]


def test_capacity_arriving_after_deadline_cannot_admit_work(tmp_path, monkeypatch):
    ledger, batch, _, quality = budgets(tmp_path)
    batch.reserve("batch-sibling-worker", "4", "worker")
    clock = [0.0]

    def late_settlement(seconds):
        clock[0] += seconds + 0.1
        ledger.settle("batch-sibling-worker", "1", evidence="late usage")

    monkeypatch.setattr(client.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(client.time, "sleep", late_settlement)
    with pytest.raises(BudgetExceeded):
        quality.reserve_with_wait("batch-child-quality-solver", "3", "solver", timeout_sec=1)
    assert len(ledger.status()["operations"]) == 1


def test_wait_configuration_is_bounded():
    assert LoopOptions().shared_reservation_wait_sec == 30
    assert LoopOptions(shared_reservation_wait_sec=0).shared_reservation_wait_sec == 0
    for value in (-1, 301, True, 0.1):
        with pytest.raises(ValueError):
            LoopOptions(shared_reservation_wait_sec=value)
