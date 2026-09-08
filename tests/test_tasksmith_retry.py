from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from repo2rlenv.tasksmith import pipeline
from repo2rlenv.tasksmith.config import TasksmithConfig
from repo2rlenv.tasksmith.models import OperationKey
from repo2rlenv.tasksmith.state import ArtifactCorrupt, ReconciliationRequired


@pytest.fixture
def candidate(tmp_path):
    source = {
        "id": "accelerate-3969",
        "repo": "huggingface/accelerate",
        "number": 3969,
        "url": "https://github.com/huggingface/accelerate/pull/3969",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    config = TasksmithConfig(
        ledger_path=tmp_path / "ledger.json", ledger_limit_usd=100, campaign_id="fixture"
    )
    return pipeline.Candidate(config, source, tmp_path / "candidate", tmp_path / "campaign")


def setup_effect(candidate):
    candidate.lease = candidate.journal.acquire_lease(
        candidate.pr.key, owner="fixture", ttl_seconds=300
    )
    state = {"revision_digest": pipeline.canonical_digest(candidate.source), "revision": 0}
    candidate.journal.register_revision(
        candidate.lease,
        revision=0,
        task_digest=state["revision_digest"],
        parent_digest=None,
        manifest={"input": candidate.source},
    )
    key = OperationKey(
        pr_id=candidate.pr.key,
        revision_digest=state["revision_digest"],
        stage="discovery",
        input_digest=pipeline.canonical_digest(candidate.source),
        policy_digest=candidate.config.digest(),
        attempt=0,
        effect="discovery",
    )
    return state, key


@pytest.mark.parametrize("status", ["claimed", "submitted", "uncertain", "failed"])
@pytest.mark.parametrize("authorization", [None, False, "true", 1])
def test_retained_effect_never_retries_without_explicit_authorization(
    candidate, status, authorization
):
    state, key = setup_effect(candidate)
    candidate.journal.claim_operation(candidate.lease, key)
    receipt = {"retry_authorized": authorization, "evidence": "fixture"}
    if status == "submitted":
        candidate.journal.record_submitted(
            candidate.lease, key.operation_id, external_id="remote-one", receipt=receipt
        )
    elif status == "uncertain":
        candidate.journal.mark_uncertain(
            candidate.lease, key.operation_id, reason="unknown terminal state", receipt=receipt
        )
    elif status == "failed":
        candidate.journal.finish_operation(
            candidate.lease,
            key.operation_id,
            failed=True,
            error="confirmed failure",
            receipt=receipt,
        )

    async def forbidden():
        pytest.fail("Retained unapproved effect was repeated")

    with pytest.raises(ReconciliationRequired, match=f"retained {status}"):
        asyncio.run(candidate.effect(state, "discovery", candidate.source, forbidden))
    assert len(candidate.journal.operations(candidate.pr.key)) == 1


def test_authorization_flag_does_not_make_uncertain_effect_retryable(candidate):
    state, key = setup_effect(candidate)
    candidate.journal.claim_operation(candidate.lease, key)
    candidate.journal.mark_uncertain(
        candidate.lease,
        key.operation_id,
        reason="terminal state not established",
        receipt={"retry_authorized": True},
    )

    async def forbidden():
        pytest.fail("Uncertain effect was retried")

    with pytest.raises(ReconciliationRequired, match="retained uncertain"):
        asyncio.run(candidate.effect(state, "discovery", candidate.source, forbidden))


@pytest.mark.parametrize("failed_stage", ["discovery", "construction", "validation"])
def test_reconciled_retry_once_uses_fresh_roots_and_preserves_identity(
    candidate, monkeypatch, failed_stage
):
    calls = {stage: [] for stage in ("discovery", "construction", "validation")}
    received = []

    def visit(stage, root, config, source, deadline):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        assert not (root / "worker-result.json").exists(), "Retry reused an occupied worker root"
        (root / "worker-result.json").write_text(stage)
        calls[stage].append(root)
        received.append((config, source, deadline))
        if stage == failed_stage and len(calls[stage]) == 1:
            raise RuntimeError("fixture lost worker response")

    async def discover(config, source, root, deadline):
        visit("discovery", root, config, source, deadline)
        return {"artifact": {"fixture": True}}

    async def construct(config, source, discovery, root, cache, deadline, repair):
        visit("construction", root, config, source, deadline)
        return {"task_path": str(root / "task"), "emitter": {"task_digest": "e" * 64}}

    async def validate(config, source, construction, root, deadline, *args):
        visit("validation", root, config, source, deadline)
        assert root.parent == Path(construction["task_path"]).parent
        assert construction["task_revision"] == 0
        assert construction["parent_task_digest"] is None
        return {"status": "accepted", "repairable": False}

    monkeypatch.setattr(pipeline, "discover", discover)
    monkeypatch.setattr(pipeline, "construct", construct)
    monkeypatch.setitem(
        sys.modules, "repo2rlenv.tasksmith.validation", SimpleNamespace(validate_candidate=validate)
    )
    with pytest.raises(RuntimeError, match="lost worker response"):
        asyncio.run(candidate.run())
    prior = next(
        r for r in candidate.journal.operations(candidate.pr.key) if r.key.stage == failed_stage
    )
    assert prior.status == "uncertain"
    lease = candidate.journal.acquire_lease(
        candidate.pr.key, owner="independent-reconciliation", ttl_seconds=300
    )
    candidate.journal.reconcile_operation(
        lease,
        prior.key.operation_id,
        failed=True,
        error="Worker incomplete; remote termination independently confirmed",
        receipt={"retry_authorized": True, "evidence": "retained cleanup and settlement receipt"},
    )
    candidate.journal.release_lease(lease)

    restarted = pipeline.Candidate(
        candidate.config, candidate.source, candidate.root, candidate.campaign_root
    )
    result = asyncio.run(restarted.run())
    assert result["status"] == "accepted"
    assert asyncio.run(restarted.run()) == result
    assert len(calls[failed_stage]) == 2
    assert calls[failed_stage][1].name == calls[failed_stage][0].name + "-attempt-1"
    assert all(len(paths) == 1 for stage, paths in calls.items() if stage != failed_stage)
    records = [
        r for r in candidate.journal.operations(candidate.pr.key) if r.key.stage == failed_stage
    ]
    first, retry = sorted(records, key=lambda r: r.key.attempt)
    assert (first.status, retry.status) == ("failed", "completed")
    assert retry.key == first.key.model_copy(update={"attempt": 1})
    assert retry.deadline == first.deadline
    assert retry.ledger == first.ledger
    assert all(row == (candidate.config, candidate.source, candidate.deadline) for row in received)
    assert not candidate.config.ledger_path.exists(), "Orchestration mutated the cost ledger"


def test_one_authorization_cannot_retry_the_next_uncertain_attempt(candidate):
    state, key = setup_effect(candidate)
    candidate.journal.claim_operation(candidate.lease, key)
    candidate.journal.reconcile_operation(
        candidate.lease,
        key.operation_id,
        failed=True,
        error="confirmed failed",
        receipt={"retry_authorized": True},
    )
    calls = []

    async def interrupted():
        calls.append(candidate.active_attempt)
        raise RuntimeError("new failure requires its own reconciliation")

    with pytest.raises(RuntimeError):
        asyncio.run(candidate.effect(state, "discovery", candidate.source, interrupted))
    with pytest.raises(ReconciliationRequired, match="retained uncertain"):
        asyncio.run(candidate.effect(state, "discovery", candidate.source, interrupted))
    assert calls == [1]


def test_completed_rejection_reuses_verified_artifacts_and_never_rerolls(candidate):
    state, key = setup_effect(candidate)

    async def rejected():
        return {"status": "rejected", "score": 2}

    original = asyncio.run(candidate.effect(state, "discovery", candidate.source, rejected))

    async def forbidden():
        pytest.fail("Completed quality rejection was rerolled")

    assert (
        asyncio.run(candidate.effect(state, "discovery", candidate.source, forbidden)) == original
    )
    record = candidate.journal.get_operation(key.operation_id)
    candidate.journal.verify_artifact(record.artifacts["result"]).write_text(
        '{"status":"accepted"}'
    )
    with pytest.raises(ArtifactCorrupt):
        asyncio.run(candidate.effect(state, "discovery", candidate.source, forbidden))


@pytest.mark.parametrize("revision", [0, 1])
def test_construction_result_records_task_revision_and_parent_digest(
    candidate, monkeypatch, revision
):
    state, _ = setup_effect(candidate)
    if revision:
        candidate.journal.register_revision(
            candidate.lease,
            revision=1,
            task_digest="e" * 64,
            parent_digest=state["revision_digest"],
            manifest={"fixture": True},
        )
        state["revision_digest"] = "e" * 64
    state.update(revision=revision, discovery={"fixture": True})

    async def construct(config, source, discovery, root, cache, deadline, repair):
        assert root == candidate.root / f"revision-{revision}"
        return {"task_path": str(root / "task"), "emitter": {"task_digest": "f" * 64}}

    monkeypatch.setattr(pipeline, "construct", construct)
    result = asyncio.run(candidate.construct_node(state))
    construction = result["construction"]
    assert construction["task_revision"] == revision
    assert construction["parent_task_digest"] == ("e" * 64 if revision else None)
    operation = next(
        r for r in candidate.journal.operations(candidate.pr.key) if r.key.stage == "construction"
    )
    retained = json.loads(
        candidate.journal.verify_artifact(operation.artifacts["result"]).read_text()
    )
    assert retained == construction
