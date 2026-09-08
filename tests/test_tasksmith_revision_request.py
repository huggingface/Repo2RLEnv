from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("langgraph.checkpoint.sqlite.aio")

from repo2rlenv.tasksmith import pipeline
from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.models import OperationKey
from repo2rlenv.tasksmith.state import ReconciliationRequired

PARENT = "e" * 64
CHILD = "f" * 64


@pytest.fixture
def case(tmp_path, monkeypatch):
    config = TasksmithConfig(
        ledger_path=tmp_path / "ledger.json", ledger_limit_usd=100, campaign_id="revision-fixture"
    )
    source = {
        "id": "accelerate-3969",
        "repo": "huggingface/accelerate",
        "number": 3969,
        "url": "https://github.com/huggingface/accelerate/pull/3969",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    c = SimpleNamespace(
        config=config,
        source=source,
        root=tmp_path / "candidate",
        campaign=tmp_path / "campaign",
        calls=[],
        child_status="accepted",
        initial_status="incomplete",
        interrupt_validation=False,
        prior_validation={},
    )

    def record(stage, config, source, root, deadline, **details):
        c.calls.append(
            {
                "stage": stage,
                "config": config,
                "source": source,
                "root": Path(root),
                "deadline": deadline,
                **details,
            }
        )

    async def discover(config, source, root, deadline):
        record("discovery", config, source, root, deadline)
        return {"artifact": {"fixture": "frozen discovery"}}

    async def construct(config, source, discovery, root, cache, deadline, repair, **prior):
        record("construction", config, source, root, deadline, repair=repair, prior=prior)
        digest = CHILD if prior else PARENT
        return {"task_path": str(root / "task"), "emitter": {"task_digest": digest}}

    async def validate(config, source, construction, root, deadline, *args):
        record("validation", config, source, root, deadline, construction=construction)
        if c.interrupt_validation:
            raise RuntimeError("Fixture lost provider outcome; reconcile before more work")
        status = c.initial_status if construction["task_revision"] == 0 else c.child_status
        return {
            "status": status,
            "repairable": False,
            "diagnostic": "Preserved controller-boundary diagnostic",
            "task_digest": construction["emitter"]["task_digest"],
            **(c.prior_validation if construction["task_revision"] == 0 else {}),
        }

    monkeypatch.setattr(pipeline, "discover", discover)
    monkeypatch.setattr(pipeline, "construct", construct)
    monkeypatch.setitem(
        sys.modules, "repo2rlenv.tasksmith.validation", SimpleNamespace(validate_candidate=validate)
    )
    return c


def candidate(c):
    return pipeline.Candidate(c.config, c.source, c.root, c.campaign)


def request(**updates):
    return {
        "parent_task_digest": PARENT,
        "reason": "The retained construction reports the helper package outside collected artifacts.",
        "evidence": "revision-0/validation/diagnostic.json: advertised helper path is absent from source_paths",
        "feedback": {
            "status": "needs_repair",
            "repairable": True,
            "required_repairs": [
                "Collect the documented helper package without weakening the task."
            ],
        },
        **updates,
    }


def stopped(c):
    run = candidate(c)
    result = asyncio.run(run.run())
    assert result["status"] == c.initial_status
    return run, result


def records(run, stage):
    return [row for row in run.journal.operations(run.pr.key) if row.key.stage == stage]


def artifact_bytes(run, operation):
    return run.journal.verify_artifact(operation.artifacts["result"]).read_bytes()


def test_stopped_incomplete_request_preserves_parent_cost_deadline_and_full_lineage(case):
    c = case
    old, original = stopped(c)
    budget = c.config.budget(c.source["id"])
    reservation = budget.reserve(0.25, "fixture retained charge; no external effect")
    budget.settle(reservation, 0.10)
    ledger_before = c.config.ledger_path.read_bytes()
    identity_before = (c.root / "identity.json").read_bytes()
    parent_validation = records(old, "validation")[0]
    prior_bytes = artifact_bytes(old, parent_validation)
    prior_record = parent_validation.model_dump()
    new = candidate(c)
    result = asyncio.run(new.run(revision_request=request()))
    assert result["status"] == "accepted" and result["revision"] == 1
    assert result["revision_digest"] == CHILD
    assert (
        original["revision_digest"] == PARENT and original["validation"]["status"] == "incomplete"
    )
    assert artifact_bytes(new, parent_validation) == prior_bytes
    assert (
        new.journal.get_operation(parent_validation.key.operation_id).model_dump() == prior_record
    )
    assert (c.root / "identity.json").read_bytes() == identity_before
    assert c.config.ledger_path.read_bytes() == ledger_before
    assert new.deadline == old.deadline
    assert [call["stage"] for call in c.calls] == [
        "discovery",
        "construction",
        "validation",
        "construction",
        "validation",
    ]
    child = c.calls[3]
    assert child["root"] == c.root / "revision-1"
    assert child["prior"] == {
        "prior_construction": original["construction"],
        "parent_task_digest": PARENT,
    }
    assert child["repair"]["diagnosis"] == request()["reason"]
    assert child["repair"]["retained_evidence"] == request()["evidence"]
    assert child["repair"]["repairable"] is True
    assert c.calls[4]["construction"]["task_revision"] == 1
    assert c.calls[4]["construction"]["parent_task_digest"] == PARENT
    assert all(
        row["config"] == c.config and row["source"] == c.source and row["deadline"] == old.deadline
        for row in c.calls
    )
    operations = new.journal.operations(new.pr.key)
    assert all(
        row.deadline == parent_validation.deadline and row.ledger == parent_validation.ledger
        for row in operations
    )
    receipt = records(new, "revision_request")
    assert len(receipt) == 1 and receipt[0].status == "completed"
    assert json.loads(artifact_bytes(new, receipt[0])) == request()
    assert receipt[0].key.revision_digest == PARENT
    assert receipt[0].key.input_digest == pipeline.canonical_digest(request())
    with new.journal._transaction() as connection:
        revisions = [
            tuple(row)
            for row in connection.execute(
                "SELECT revision, task_digest, parent_digest FROM revisions WHERE pr_id=? ORDER BY revision",
                (new.pr.key,),
            )
        ]
    assert revisions == [
        (0, pipeline.canonical_digest(c.source), None),
        (1, PARENT, pipeline.canonical_digest(c.source)),
        (2, CHILD, PARENT),
    ]
    assert new.journal.counts()["attempted_prs"] == 1
    calls_before = list(c.calls)
    assert asyncio.run(candidate(c).run(revision_request=request())) == result
    assert c.calls == calls_before and len(records(new, "revision_request")) == 1
    assert new.journal.counts()["revisions"] == 3


@pytest.mark.parametrize("boundary", ["request_commit", "feedback_checkpoint", "repair_checkpoint"])
def test_same_request_resumes_once_across_checkpoint_windows(case, monkeypatch, boundary):
    c = case
    old, _ = stopped(c)
    prior = records(old, "validation")[0]
    prior_bytes = artifact_bytes(old, prior)
    if boundary == "request_commit":
        original = pipeline.Candidate.effect

        async def interrupt(self, state, stage, inputs, execute):
            result = await original(self, state, stage, inputs, execute)
            if stage == "revision_request":
                raise RuntimeError("Fixture checkpoint interruption")
            return result

        name = "effect"
    elif boundary == "feedback_checkpoint":
        original = pipeline.Candidate.repair_node

        async def interrupt(self, state):
            raise RuntimeError("Fixture checkpoint interruption")

        name = "repair_node"
    else:
        original = pipeline.Candidate.construct_node

        async def interrupt(self, state):
            raise RuntimeError("Fixture checkpoint interruption")

        name = "construct_node"
    monkeypatch.setattr(pipeline.Candidate, name, interrupt)
    with pytest.raises(RuntimeError, match="checkpoint interruption"):
        asyncio.run(candidate(c).run(revision_request=request()))
    assert len(c.calls) == 3 and len(records(old, "revision_request")) == 1
    receipt_before = artifact_bytes(old, records(old, "revision_request")[0])
    monkeypatch.setattr(pipeline.Candidate, name, original)
    final = asyncio.run(candidate(c).run(revision_request=request()))
    assert final["status"] == "accepted" and final["revision"] == 1
    assert len(c.calls) == 5 and len(records(old, "revision_request")) == 1
    assert artifact_bytes(old, records(old, "revision_request")[0]) == receipt_before
    assert artifact_bytes(old, prior) == prior_bytes


@pytest.mark.parametrize(
    "updates,message",
    [
        ({"parent_task_digest": "d" * 64}, "current immutable task"),
        ({"reason": " "}, "concrete diagnosis"),
        ({"evidence": ""}, "retained evidence"),
        ({"evidence": []}, "retained evidence"),
        ({"feedback": {"status": "needs_repair", "repairable": False}}, "repairable feedback"),
        ({"feedback": {"status": "accepted", "repairable": True}}, "never acceptance"),
    ],
)
def test_invalid_request_rejected_before_any_new_effect(case, updates, message):
    c = case
    old, _ = stopped(c)
    before = old.journal.operations(old.pr.key)
    with pytest.raises(ValueError, match=message):
        asyncio.run(candidate(c).run(revision_request=request(**updates)))
    assert len(c.calls) == 3
    assert old.journal.operations(old.pr.key) == before


def test_missing_request_field_and_accepted_reroll_rejected_before_any_new_effect(case):
    c = case
    c.initial_status = "accepted"
    old, _ = stopped(c)
    missing = request()
    del missing["evidence"]
    with pytest.raises(ValueError, match="needs parent digest"):
        asyncio.run(candidate(c).run(revision_request=missing))
    with pytest.raises(ValueError, match="accepted candidate cannot be rerolled"):
        asyncio.run(candidate(c).run(revision_request=request()))
    assert len(c.calls) == 3 and not records(old, "revision_request")


def test_active_uncertain_graph_stage_requires_reconciliation_before_request(case):
    c = case
    c.interrupt_validation = True
    old = candidate(c)
    with pytest.raises(RuntimeError, match="lost provider outcome"):
        asyncio.run(old.run())
    assert records(old, "validation")[0].status == "uncertain"
    with pytest.raises(ReconciliationRequired, match="active stage"):
        asyncio.run(candidate(c).run(revision_request=request()))
    assert len(c.calls) == 3 and not records(old, "revision_request")


@pytest.mark.parametrize("limit", ["revisions", "deadline", "budget"])
def test_original_limits_block_new_revision_before_any_new_effect(case, monkeypatch, limit):
    c = case
    if limit == "revisions":
        c.config = c.config.model_copy(update={"max_revisions": 1})
    old, _ = stopped(c)
    if limit == "deadline":
        monkeypatch.setattr(pipeline.time, "time", lambda: old.deadline - 599)
    elif limit == "budget":
        budget = c.config.budget(c.source["id"])
        budget.reserve(
            c.config.candidate_limit_usd - c.config.validation_reserve_usd,
            "fixture existing unresolved reservation",
        )
    before = old.journal.operations(old.pr.key)
    with pytest.raises(ValueError, match="Original revision, deadline or budget limit"):
        asyncio.run(candidate(c).run(revision_request=request()))
    assert len(c.calls) == 3 and old.journal.operations(old.pr.key) == before


def test_uncertain_child_operation_under_terminal_graph_blocks_new_revision(case):
    c = case
    old, _ = stopped(c)
    lease = old.journal.acquire_lease(
        old.pr.key, owner="fixture provider reconciliation", ttl_seconds=300
    )
    key = OperationKey(
        pr_id=old.pr.key,
        revision_digest=PARENT,
        stage="trial",
        input_digest="c" * 64,
        policy_digest=c.config.digest(),
        attempt=0,
        effect="solver-workspace",
    )
    old.journal.claim_operation(lease, key)
    old.journal.mark_uncertain(lease, key.operation_id, reason="Provider cleanup not confirmed")
    old.journal.release_lease(lease)
    with pytest.raises(ReconciliationRequired):
        asyncio.run(candidate(c).run(revision_request=request()))
    assert len(c.calls) == 3 and not records(old, "revision_request")


@pytest.mark.parametrize("container", ["all_trials", "outcome"])
@pytest.mark.parametrize("cleanup", [False, None, "true", "missing"])
def test_unresolved_prior_trial_resources_block_request_before_new_effect(case, container, cleanup):
    c = case
    row = {"provider_resource_id": "retained-fixture-sandbox", "status": "incomplete"}
    if cleanup != "missing":
        row["cleanup_confirmed"] = cleanup
    c.prior_validation = {
        container: {"baseline": {"cleanup_confirmed": True}, "oracle": row}
        if container == "all_trials"
        else row
    }
    old, _ = stopped(c)
    validation = records(old, "validation")[0]
    prior_bytes = artifact_bytes(old, validation)
    with pytest.raises(ReconciliationRequired, match="prior trial resources"):
        asyncio.run(candidate(c).run(revision_request=request()))
    assert len(c.calls) == 3 and not records(old, "revision_request")
    assert artifact_bytes(old, validation) == prior_bytes


def test_confirmed_prior_resource_cleanup_permits_the_same_bounded_revision(case):
    c = case
    c.prior_validation = {
        "all_trials": {
            "baseline": {"cleanup_confirmed": True},
            "oracle": {"cleanup_confirmed": True},
        },
        "outcome": {"cleanup_confirmed": True, "status": "incomplete"},
    }
    old, _ = stopped(c)
    result = asyncio.run(candidate(c).run(revision_request=request()))
    assert result["revision"] == 1 and result["status"] == "accepted"
    assert len(c.calls) == 5 and len(records(old, "revision_request")) == 1
