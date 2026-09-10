from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from repo2rlenv.campaigns.budget import BudgetExceeded, BudgetLedger, OperationAlreadyRecorded


def test_uncertain_operations_stay_reserved_across_restart(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.db", limit_usd="1")
    ledger.reserve("request", "0.75", "model request")
    ledger.mark_uncertain("request", "connection lost after dispatch")
    resumed = BudgetLedger(ledger.path)
    assert resumed.status()["remaining_usd"] == "0.250000"
    with pytest.raises(OperationAlreadyRecorded):
        resumed.reserve("request", "0.75", "retry")
    with pytest.raises(BudgetExceeded):
        resumed.reserve("second", "0.30", "another request")
    resumed.settle("request", "0.50", evidence="provider usage receipt")
    assert resumed.status()["remaining_usd"] == "0.500000"


def test_concurrent_reservations_cannot_overspend(tmp_path):
    path = tmp_path / "budget.db"
    BudgetLedger(path, limit_usd="1")

    def reserve(i):
        try:
            BudgetLedger(path).reserve(str(i), "0.60", "worker")
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(reserve, range(4)))
    assert sum(results) == 1
    assert BudgetLedger(path).status()["reserved_usd"] == "0.600000"


def test_actual_overrun_is_not_hidden(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.db", limit_usd="1")
    ledger.reserve("request", "0.50", "provider")
    ledger.settle("request", "1.20", evidence="provider invoice")
    assert ledger.status()["remaining_usd"] == "-0.200000"
    with pytest.raises(BudgetExceeded):
        ledger.reserve("second", "0.01", "provider")
    ledger.settle("request", "1.20", evidence="provider invoice")
    with pytest.raises(OperationAlreadyRecorded):
        ledger.settle("request", "0.20", evidence="incorrect")


def test_opening_ledger_cannot_reset_limit(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.db", limit_usd="1")
    with pytest.raises(ValueError, match="implicit budget reset"):
        BudgetLedger(ledger.path, limit_usd="100")


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1"])
def test_invalid_amounts_rejected(amount, tmp_path):
    with pytest.raises(ValueError, match="finite and nonnegative"):
        BudgetLedger(tmp_path / "budget.db", limit_usd=amount)
