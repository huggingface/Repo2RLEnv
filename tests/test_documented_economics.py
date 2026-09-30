"""Protect public cost accounting against misleading aggregation changes."""

import copy
import importlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def accounting(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "docs/_tools"))
    module = importlib.import_module("economics")

    def load(name):
        return json.loads((ROOT / "docs/data" / name).read_text())

    return (
        module,
        load("pipelines.json"),
        load("experiment-economics.json"),
        load("frontiersmith-campaign.json"),
    )


def test_scoped_costs_reconcile(accounting):
    module, data, audit, frontier = accounting
    module.validate(data, audit, frontier)
    text = "\n".join(module.program_table(data, audit, frontier))
    assert "$1,586.71" in text
    assert "$31.72" in text
    assert module.money(None) == "n/a"
    assert module.money("0") == "$0.00"


def test_parent_transfer_is_not_an_extra_expense(accounting):
    module, data, audit, frontier = accounting
    audit = copy.deepcopy(audit)
    group = audit["expansion_groups"][0]
    group["stages"]["subcampaign"] = {"accounted_usd": "0", "held_usd": "0"}
    with pytest.raises(ValueError, match="parent transfer"):
        module.validate(data, audit, frontier)


def test_recipe_cost_must_match_recorded_sample(accounting):
    module, data, audit, frontier = accounting
    data = copy.deepcopy(data)
    data["pipelines"][0]["model_usd"] = "0"
    with pytest.raises(ValueError, match="API costs disagree"):
        module.validate(data, audit, frontier)


def test_token_coverage_cannot_exceed_settled_calls(accounting):
    module, data, audit, frontier = accounting
    audit = copy.deepcopy(audit)
    model = audit["recipe_expansion"]["r2e"]["models"][0]
    model["receipts_with_tokens"] = model["settled_calls"] + 1
    with pytest.raises(ValueError, match="token receipt coverage"):
        module.validate(data, audit, frontier)


def test_unknown_charge_is_not_silently_dropped(accounting):
    module, data, audit, frontier = accounting
    audit = copy.deepcopy(audit)
    audit["recipe_expansion"]["tmax"]["held_usd"] = "0"
    with pytest.raises(ValueError, match="API holds disagree"):
        module.validate(data, audit, frontier)


def test_duplicate_ledger_group_is_rejected(accounting):
    module, data, audit, frontier = accounting
    audit = copy.deepcopy(audit)
    audit["expansion_groups"].append(audit["expansion_groups"][0])
    with pytest.raises(ValueError, match="Duplicate"):
        module.validate(data, audit, frontier)
