from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from repo2rlenv.curation.budget import Budget, BudgetExceeded, register_scope_constraints


def rule(*, scopes=(), groups=(), limit=5):
    return {"scopes": list(scopes), "groups": list(groups), "limit_usd": limit}


def bind(budget, constraints):
    with budget._locked() as state:
        register_scope_constraints(state, "child", constraints)


@pytest.mark.parametrize("gate", ["lineage", "phase"])
def test_plain_reconstructed_budget_enforces_persisted_repair_caps(tmp_path, gate):
    path = tmp_path / "ledger.json"
    parent = Budget(path, 100, scope="parent", group="old-batch")
    parent.reserve(3, "outstanding parent call")
    child = Budget(path, 100, scope="child", scope_limit=50, group="new-batch", group_limit=50)
    constraints = {
        "repair_lineage": rule(scopes=["parent", "child"], limit=5 if gate == "lineage" else 50),
        "repair_phase": rule(groups=["old-batch", "new-batch"], limit=5 if gate == "phase" else 50),
    }
    bind(child, constraints)
    # A subprocess reconstructs plain Budget, with no wrapper or group arguments.
    restarted = Budget(path, 100, scope="child")
    key = restarted.reserve(2, "solver call")
    before = path.read_bytes()
    with pytest.raises(BudgetExceeded, match=f"Repair {gate} allowance"):
        Budget(path, 100, scope="child").reserve(0.01, "cannot exceed combined cap")
    assert path.read_bytes() == before
    restarted.settle(key, 3)
    entries = json.loads(path.read_text())["entries"]
    assert entries[key]["charged_usd"] == 3 and entries[key]["overrun"] is True
    with pytest.raises(BudgetExceeded):
        restarted.reserve(0.01, "paid overrun remains counted")


def test_union_membership_counts_each_entry_once_and_keeps_refunds(tmp_path):
    path = tmp_path / "ledger.json"
    parent = Budget(path, 100, scope="parent", group="batch")
    key = parent.reserve(4, "parent")
    parent.settle(key, 2)
    child = Budget(path, 100, scope="child")
    bind(child, {"combined": rule(scopes=["parent"], groups=["batch"], limit=3)})
    child.reserve(1, "fits after actual settlement and deduplicated membership")
    with pytest.raises(BudgetExceeded):
        child.reserve(0.01, "full")


def test_registration_is_idempotent_immutable_and_preserves_entries(tmp_path):
    budget = Budget(tmp_path / "ledger.json", 100, scope="child")
    policy = {"lineage": rule(scopes=["parent", "child"])}
    bind(budget, policy)
    budget.reserve(1, "first charge")
    before = budget.path.read_bytes()
    bind(budget, {"lineage": rule(scopes=["child", "parent"])})
    assert budget.path.read_bytes() == before
    with pytest.raises(ValueError, match="different policy"):
        bind(budget, {"lineage": rule(scopes=["child", "parent"], limit=6)})
    assert budget.path.read_bytes() == before


def test_missing_registration_after_even_zero_settled_charge_fails_closed(tmp_path):
    budget = Budget(tmp_path / "ledger.json", 100, scope="child")
    key = budget.reserve(1, "historical charge")
    budget.settle(key, 0)
    before = budget.path.read_bytes()
    with pytest.raises(ValueError, match="after charges exist"):
        bind(budget, {"lineage": rule(scopes=["parent", "child"])})
    assert budget.path.read_bytes() == before


@pytest.mark.parametrize(
    "registry",
    [
        None,
        [],
        {"child": {}},
        {"child": {"x": {}}},
        {"child": {"x": rule(scopes=["parent"], limit=float("nan"))}},
        {"child": {"x": rule(scopes=["parent"], limit=True)}},
        {"child": {"x": rule(scopes=["parent", "parent"])}},
        {"child": {"x": rule(scopes=[{}])}},
        {"child": {"x": rule()}},
        {"child": {"x": {**rule(scopes=["parent"]), "extra": 1}}},
    ],
)
def test_malformed_persisted_constraints_fail_before_any_charge(tmp_path, registry):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"entries": {}, "scope_constraints": registry}))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        Budget(path, 100, scope="child").reserve(1, "blocked")
    assert path.read_bytes() == before


def test_concurrent_plain_instances_share_constraint_lock(tmp_path):
    path = tmp_path / "ledger.json"
    bind(Budget(path, 100), {"lineage": rule(scopes=["child"], limit=3)})

    def reserve(_):
        try:
            Budget(path, 100, scope="child").reserve(1, "parallel")
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(16))) == 3
    assert len(json.loads(path.read_text())["entries"]) == 3


def test_unregistered_scope_semantics_are_unchanged(tmp_path):
    path = tmp_path / "ledger.json"
    old = Budget(path, 10, scope="legacy", scope_limit=8)
    old.reserve(6, "legacy")
    bind(Budget(path, 10), {"lineage": rule(scopes=["child"], limit=1)})
    old.reserve(2, "unrelated policy does not impose a new limit")
    with pytest.raises(BudgetExceeded, match="Candidate budget"):
        old.reserve(1, "original scope cap")
    assert old.spent == 8
