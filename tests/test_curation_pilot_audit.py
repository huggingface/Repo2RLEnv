from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest

from repo2rlenv.curation import campaign, pilot
from repo2rlenv.curation.budget import Budget
from repo2rlenv.curation.models import CampaignConfig, Contract
from repo2rlenv.curation.protocol import DraftLimitExceeded, check_verification_plan


def _protocol(tmp_path):
    ledger = tmp_path / "production.json"
    Budget(ledger, 380).reserve(1, "retained previous cost")
    return {
        "id": "independent-audit",
        "runtime_digest": pilot.runtime_digest(),
        "production_limit_usd": 380,
        "ledger": str(ledger),
        "config": CampaignConfig(
            target=5,
            budget_usd=40,
            max_candidate_usd=8,
            max_revisions=2,
            max_candidate_drafts=2,
            require_verification_plan=True,
            specification_review=True,
            verifier_review=True,
        ).model_dump(),
        "sources": [
            {
                "url": f"https://github.com/a/b/pull/{number}",
                "repo": "a/b",
                "id": f"a-b-{number}",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
            }
            for number in range(1, 6)
        ],
    }


@pytest.mark.asyncio
async def test_same_pilot_cannot_dispatch_twice_from_concurrent_output_directories(
    tmp_path, monkeypatch
):
    protocol = _protocol(tmp_path)
    path = tmp_path / "input.json"
    path.write_text(json.dumps(protocol))
    calls = []

    async def curate(source, root, config, budget):
        calls.append(source["url"])
        budget.reserve(0.1, "mock author reservation")
        await asyncio.sleep(0)
        raise DraftLimitExceeded("finished bounded mock attempt")

    monkeypatch.setattr(pilot, "curate_one", curate)
    results = await asyncio.gather(
        pilot.run_pilot(path, tmp_path / "output-a"),
        pilot.run_pilot(path, tmp_path / "output-b"),
        return_exceptions=True,
    )
    assert len(calls) == len(set(calls)) == 5
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, (ValueError, RuntimeError)) for result in results) == 1


@pytest.mark.asyncio
async def test_pilot_output_claim_survives_zero_cost_deferrals(tmp_path, monkeypatch):
    protocol = _protocol(tmp_path)
    path = tmp_path / "input.json"
    path.write_text(json.dumps(protocol))
    calls = []

    async def curate(source, root, config, budget):
        calls.append(source["url"])
        raise campaign.CandidateDeferred("unsuitable before any model or cloud reservation")

    monkeypatch.setattr(pilot, "curate_one", curate)
    original = tmp_path / "original"
    await pilot.run_pilot(path, original)
    await pilot.run_pilot(path, original)
    with pytest.raises((ValueError, RuntimeError)):
        await pilot.run_pilot(path, tmp_path / "replacement")
    assert len(calls) == 5


@pytest.mark.asyncio
async def test_unexportable_structural_submissions_exhaust_the_same_two_draft_limit(
    tmp_path, monkeypatch
):
    exports = []
    stopped = []

    class Sandbox:
        def __init__(self, timeout):
            self.sandbox = SimpleNamespace(object_id="offline-audit")

        async def start(self):
            pass

        async def prepare(self, source):
            pass

        async def stop(self):
            stopped.append(True)

        async def shell(self, **kwargs):
            return ""

        async def export(self, task):
            exports.append(task)
            raise ValueError("Cannot export candidate: symlink in submitted task")

    async def author(**kwargs):
        validate = kwargs["handlers"]["validate_candidate"]
        for _ in range(3):
            try:
                await validate()
            except ValueError:
                # Both production agent runtimes treat ValueError as recoverable tool input.
                pass
        pytest.fail("Three unexportable drafts bypassed the limit")

    monkeypatch.setattr(campaign, "AuthorSandbox", Sandbox)
    monkeypatch.setattr(campaign, "run_agent", author)
    with pytest.raises(DraftLimitExceeded):
        await campaign.curate_one(
            {"id": "audit", "url": "https://github.com/a/b/pull/1"},
            tmp_path / "candidate",
            CampaignConfig(max_candidate_drafts=2),
            Budget(tmp_path / "ledger.json", 20),
        )
    assert len(exports) == 2
    assert stopped == [True]


def _contract_and_plan():
    contract = Contract(
        title="Observe two public behaviors",
        rationale="Audit structural plan mapping",
        source_paths=["src/package"],
        min_tests=3,
        requirements=[
            {"id": "sum", "behavior": "Return the independent sum", "tests": ["test_sum"]},
            {"id": "order", "behavior": "Preserve order", "tests": ["test_order", "test_empty"]},
        ],
        mutations=[
            {"name": name, "rationale": "Break observable behavior", "script": "exit 0"}
            for name in ("wrong_sum", "wrong_order")
        ],
        equivalents=[{"name": "loop", "rationale": "Equivalent loop", "script": "exit 0"}],
    )
    plan = {
        "behaviors": [
            {
                "requirement": req.id,
                "expected_result": "Compute fixed independent expected values from the original inputs.",
                "tests": req.tests,
                "mutations": [mutation.name],
                "equivalents": ["loop"],
            }
            for req, mutation in zip(contract.requirements, contract.mutations, strict=True)
        ],
        "offline_dependencies": "Use only pinned packages and local tiny fixtures.",
        "artifact_boundary": "Collect the package directory including sibling helpers.",
    }
    return contract, plan


@pytest.mark.parametrize(
    "field,value",
    [
        ("mutations", []),
        ("mutations", ["absent_negative"]),
        ("equivalents", []),
        ("equivalents", ["absent_positive"]),
        ("tests", ["unmapped_test"]),
        ("requirement", "unknown_requirement"),
    ],
)
def test_verification_plan_rejects_missing_or_wrong_behavior_mapping(tmp_path, field, value):
    contract, plan = _contract_and_plan()
    path = tmp_path / "verification-plan.json"
    path.write_text(json.dumps(plan))
    check_verification_plan(tmp_path, contract)
    plan["behaviors"][0][field] = value
    path.write_text(json.dumps(plan))
    with pytest.raises(ValueError):
        check_verification_plan(tmp_path, contract)


@pytest.mark.parametrize("extra", ["verification-plan.json", "authoring-context.json"])
def test_verifier_snapshot_and_read_coverage_include_private_authoring_evidence(tmp_path, extra):
    verifier = importlib.import_module("repo2rlenv.curation.verifier_review")
    _, plan = _contract_and_plan()
    files = {
        "instruction.md": "Compute sum and preserve order.",
        "contract.json": "{}",
        "tests/test_contract.py": "def test_sum():\n    assert 1 + 2 == 3\n",
        extra: json.dumps(plan),
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    snapshot = verifier._snapshot(tmp_path)
    assert snapshot[extra] == files[extra]
    reads = {name: [[0, len(text)]] for name, text in snapshot.items()}
    assert verifier._complete(snapshot, reads)
    del reads[extra]
    assert not verifier._complete(snapshot, reads)
    (tmp_path / extra).write_text(json.dumps({**plan, "artifact_boundary": "different"}))
    assert verifier._snapshot(tmp_path) != snapshot
