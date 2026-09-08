from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from repo2rlenv.curation import campaign, pilot
from repo2rlenv.curation.budget import Budget, BudgetExceeded
from repo2rlenv.curation.design import DesignNotSubmitted
from repo2rlenv.curation.models import CampaignConfig
from repo2rlenv.curation.protocol import DraftLimitExceeded, DraftTracker


def test_batch_cap_shares_global_costs_and_survives_restart(tmp_path):
    ledger = tmp_path / "budget.json"
    Budget(ledger, 100).reserve(10, "previous campaign")

    def attempt(i):
        try:
            Budget(ledger, 100, scope=str(i), scope_limit=8, group="pilot", group_limit=40).reserve(
                8, "slot"
            )
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(attempt, range(10))) == 5
    assert Budget(ledger, 100).spent == 50
    with pytest.raises(BudgetExceeded, match="Batch budget"):
        Budget(ledger, 100, scope="new", scope_limit=8, group="pilot", group_limit=40).reserve(
            1, "restart"
        )
    with pytest.raises(BudgetExceeded, match="Budget"):
        Budget(ledger, 50, group="other", group_limit=40).reserve(1, "another group")


def test_distinct_submission_limit_is_durable(tmp_path):
    path = tmp_path / "drafts.json"
    tracker = DraftTracker(path, 2)
    tracker.observe("first", tmp_path)
    tracker.observe("first", tmp_path)
    tracker.observe("repair", tmp_path)
    with pytest.raises(DraftLimitExceeded):
        DraftTracker(path, 2).observe("third", tmp_path)
    assert len(json.loads(path.read_text())) == 2


@pytest.mark.asyncio
async def test_author_cannot_bypass_limit_with_repeated_validation_tools(tmp_path, monkeypatch):
    stopped = []

    class Sandbox:
        def __init__(self, timeout):
            self.sandbox = SimpleNamespace(object_id="fake")
            self.exports = 0

        async def start(self):
            pass

        async def prepare(self, source):
            pass

        async def stop(self):
            stopped.append(True)

        async def shell(self, **kwargs):
            return ""

        async def export(self, task):
            self.exports += 1
            task.mkdir(parents=True)
            (task / "partial.txt").write_text(str(self.exports))

    def invalid(*args):
        raise ValueError("Missing required files")

    async def author(**kwargs):
        validate = kwargs["handlers"]["validate_candidate"]
        assert "Structural" in await validate()
        await validate()
        pytest.fail("A second failed draft must terminate before a third repair")

    monkeypatch.setattr(campaign, "AuthorSandbox", Sandbox)
    monkeypatch.setattr(campaign, "finalize", invalid)
    monkeypatch.setattr(campaign, "run_agent", author)
    with pytest.raises(DraftLimitExceeded):
        await campaign.curate_one(
            {"id": "test", "url": "https://github.com/a/b/pull/1"},
            tmp_path / "candidate",
            CampaignConfig(max_candidate_drafts=2),
            Budget(tmp_path / "ledger.json", 20),
        )
    assert stopped == [True]
    assert len(json.loads((tmp_path / "candidate/submitted-drafts.json").read_text())) == 2
    assert (
        "Missing required files" in (tmp_path / "candidate/repair-limit-feedback.json").read_text()
    )


def protocol(tmp_path):
    ledger = tmp_path / "production.json"
    Budget(ledger, 380).reserve(1, "past")
    return {
        "id": "test-five",
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
                "url": f"https://github.com/a/b/pull/{i}",
                "repo": "a/b",
                "id": f"a-b-{i}",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
            }
            for i in range(1, 6)
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,status", [(DraftLimitExceeded, "repair_limit"), (DesignNotSubmitted, "design_failure")]
)
async def test_pilot_keeps_failures_in_denominator_and_never_reruns(
    tmp_path, monkeypatch, error, status
):
    data = protocol(tmp_path)
    path = tmp_path / "input.json"
    path.write_text(json.dumps(data))
    calls = []

    async def curate(source, root, config, budget):
        calls.append(source["id"])
        budget.reserve(0.2, "unknown failed call")
        raise error("bounded phase exhausted")

    monkeypatch.setattr(pilot, "curate_one", curate)
    out = tmp_path / "pilot"
    result = await pilot.run_pilot(path, out)
    assert len(result["rows"]) == 5
    assert result["charged_or_reserved_usd"] == 1
    assert {r["status"] for r in result["rows"]} == {status}
    await pilot.run_pilot(path, out)
    assert len(calls) == 5
    with pytest.raises(ValueError, match="original output"):
        await pilot.run_pilot(path, tmp_path / "new-output")


@pytest.mark.asyncio
async def test_interrupted_slot_is_counted_without_spending_again(tmp_path, monkeypatch):
    data = protocol(tmp_path)
    path = tmp_path / "input.json"
    path.write_text(json.dumps(data))
    out = tmp_path / "pilot"
    out.mkdir()
    (out / "protocol.json").write_text(json.dumps(data))
    rows = [{"source": s["url"], "status": "running"} for s in data["sources"]]
    (out / "manifest.json").write_text(json.dumps({"rows": rows}))

    async def forbidden(*args, **kwargs):
        pytest.fail("Interrupted slots must not relaunch")

    monkeypatch.setattr(pilot, "curate_one", forbidden)
    result = await pilot.run_pilot(path, out)
    assert len(result["rows"]) == 5
    assert {r["status"] for r in result["rows"]} == {"interrupted"}


def test_changed_runtime_or_relaxed_repair_limits_are_rejected(tmp_path):
    data = protocol(tmp_path)
    data["runtime_digest"] = "changed"
    with pytest.raises(ValueError, match="runtime changed"):
        pilot.validate_protocol(data)
    data["runtime_digest"] = pilot.runtime_digest()
    data["config"]["max_candidate_drafts"] = 3
    with pytest.raises(ValueError, match="two drafts"):
        pilot.validate_protocol(data)
