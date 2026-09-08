from __future__ import annotations

import concurrent.futures
from types import SimpleNamespace

import pytest

from repo2rlenv.curation.budget import Budget
from repo2rlenv.tasksmith.models import (
    QUALITY_DIMENSIONS,
    AdmissionContext,
    Deadline,
    LedgerRef,
    OperationKey,
    PRIdentity,
    QualityReport,
)
from repo2rlenv.tasksmith.state import (
    ArtifactCorrupt,
    Journal,
    JournalConflict,
    LeaseBusy,
    LeaseLost,
    ReconciliationRequired,
)

D = "a" * 64


@pytest.fixture
def setup(tmp_path):
    now = [1000.0]
    journal = Journal(tmp_path / "operations.sqlite", clock=lambda: now[0])
    pr = PRIdentity(repository="example/library", number=12)
    deadline = Deadline(expires_at=2000)
    ledger = LedgerRef(path=str(tmp_path / "budget.json"), scope="original:pr12", group="pilot")
    journal.register_pr(pr, deadline=deadline, ledger=ledger)
    lease = journal.acquire_lease(pr.key, owner="worker-one", ttl_seconds=60)
    journal.register_revision(
        lease, revision=0, task_digest=D, parent_digest=None, manifest={"source": "pinned"}
    )
    key = OperationKey(
        pr_id=pr.key,
        revision_digest=D,
        stage="construct",
        input_digest="b" * 64,
        policy_digest="c" * 64,
        attempt=0,
        effect="remote-command",
    )
    return SimpleNamespace(
        journal=journal, now=now, pr=pr, deadline=deadline, ledger=ledger, lease=lease, key=key
    )


def test_claim_is_durable_and_replay_never_launches_a_second_effect(setup):
    s = setup
    first = s.journal.claim_operation(s.lease, s.key)
    assert first.acquired
    restored = Journal(s.journal.path, clock=lambda: s.now[0])
    assert not restored.claim_operation(s.lease, s.key).acquired
    assert restored.get_operation(s.key.operation_id) == first.operation
    assert len(restored.events(s.key.operation_id)) == 1


def test_atomic_claim_across_concurrent_controllers(setup):
    s = setup

    def claim(_):
        return (
            Journal(s.journal.path, clock=lambda: s.now[0]).claim_operation(s.lease, s.key).acquired
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(claim, range(4)))
    assert results.count(True) == 1
    assert len(s.journal.operations(s.pr.key)) == 1


def test_lease_prevents_parallel_workers_and_fences_expired_claim(setup):
    s = setup
    s.journal.claim_operation(s.lease, s.key)
    with pytest.raises(LeaseBusy):
        s.journal.acquire_lease(s.pr.key, owner="another-controller", ttl_seconds=60)
    s.now[0] += 61
    replacement = s.journal.acquire_lease(s.pr.key, owner="replacement", ttl_seconds=60)
    assert replacement.token != s.lease.token
    assert s.journal.get_operation(s.key.operation_id).status == "uncertain"
    with pytest.raises(LeaseLost):
        s.journal.finish_operation(s.lease, s.key.operation_id, receipt={"done": True})
    with pytest.raises(ReconciliationRequired):
        s.journal.finish_operation(replacement, s.key.operation_id, receipt={"done": True})
    with pytest.raises(ReconciliationRequired):
        s.journal.claim_operation(replacement, s.key.model_copy(update={"attempt": 1}))
    assert not s.journal.claim_operation(replacement, s.key).acquired


def test_crash_after_provider_create_before_checkpoint_retains_late_id(setup):
    s = setup
    s.journal.claim_operation(s.lease, s.key)
    # Provider create returned after the controller's lease expired. No second create is allowed.
    s.now[0] += 61
    replacement = s.journal.acquire_lease(s.pr.key, owner="recovery", ttl_seconds=60)
    operation = s.journal.record_submitted(
        s.lease,
        s.key.operation_id,
        external_id="sandbox-123",
        receipt={"provider": "modal", "observed": "running"},
        reservation_ids=["reservation-original"],
    )
    assert operation.status == "uncertain" and operation.external_id == "sandbox-123"
    assert operation.reservation_ids == ["reservation-original"]
    complete = s.journal.reconcile_operation(
        replacement,
        s.key.operation_id,
        receipt={"provider_id": "sandbox-123", "observed": "stopped"},
    )
    assert complete.status == "completed"
    assert not s.journal.claim_operation(replacement, s.key).acquired
    assert [row["event"] for row in s.journal.events(s.key.operation_id)] == [
        "claimed",
        "lease_ended",
        "submitted_receipt",
        "reconciled",
    ]


def test_submitted_uncertain_and_completed_effects_cannot_be_rerolled(setup):
    s = setup
    s.journal.claim_operation(s.lease, s.key)
    s.journal.record_submitted(
        s.lease, s.key.operation_id, external_id="review-request", receipt={"submitted": True}
    )
    retry = s.key.model_copy(update={"attempt": 1})
    with pytest.raises(ReconciliationRequired):
        s.journal.claim_operation(s.lease, retry)
    # A completed rejecting review is still a completed effect, not an execution failure.
    s.journal.finish_operation(s.lease, s.key.operation_id, receipt={"review": "reject"})
    with pytest.raises(JournalConflict, match="not rerolled"):
        s.journal.claim_operation(s.lease, retry, retry_of=s.key.operation_id)
    with pytest.raises(JournalConflict, match="immutable"):
        s.journal.finish_operation(s.lease, s.key.operation_id, receipt={"review": "accept"})


def test_confirmed_failed_effect_retry_is_explicit_and_preserves_attempt_history(setup):
    s = setup
    s.journal.claim_operation(s.lease, s.key)
    s.journal.finish_operation(
        s.lease,
        s.key.operation_id,
        receipt={"provider": "confirmed not created"},
        failed=True,
        error="Provider rejected request before creation",
    )
    retry = s.key.model_copy(update={"attempt": 1})
    with pytest.raises(JournalConflict, match="explicitly reference"):
        s.journal.claim_operation(s.lease, retry)
    assert s.journal.claim_operation(s.lease, retry, retry_of=s.key.operation_id).acquired
    assert [row.status for row in s.journal.operations(s.pr.key)] == ["failed", "claimed"]


def test_original_deadline_and_budget_binding_cannot_reset_on_resume(setup):
    s = setup
    with pytest.raises(JournalConflict, match="deadline or ledger"):
        s.journal.register_pr(s.pr, deadline=Deadline(expires_at=3000), ledger=s.ledger)
    with pytest.raises(JournalConflict, match="deadline or ledger"):
        s.journal.register_pr(
            s.pr, deadline=s.deadline, ledger=s.ledger.model_copy(update={"scope": "fresh-cap"})
        )
    s.now[0] = 2001
    cleanup = s.journal.acquire_lease(s.pr.key, owner="cleanup-only", ttl_seconds=20)
    assert cleanup.deadline == s.deadline
    with pytest.raises(TimeoutError):
        s.journal.claim_operation(cleanup, s.key)


def test_journal_preserves_authoritative_budget_bytes_including_uncertain_reservations(setup):
    s = setup
    budget = Budget(s.ledger.path, 20, scope=s.ledger.scope, group=s.ledger.group)
    reservation = budget.reserve(0.5, "existing paid effect")
    before = budget.path.read_bytes()
    s.journal.claim_operation(s.lease, s.key)
    s.journal.record_submitted(
        s.lease,
        s.key.operation_id,
        external_id="existing-request",
        receipt={},
        reservation_ids=[reservation],
    )
    s.journal.mark_uncertain(
        s.lease, s.key.operation_id, reason="Lost response; retain original reservation"
    )
    s.journal.release_lease(s.lease)
    assert budget.path.read_bytes() == before
    assert budget.spent == 0.5
    assert s.journal.operations(s.pr.key)[0].ledger == s.ledger


def test_revision_and_evidence_hash_changes_never_reuse_an_old_stage(setup):
    s = setup
    s.journal.claim_operation(s.lease, s.key)
    s.journal.finish_operation(s.lease, s.key.operation_id, receipt={"result": "done"})
    s.journal.register_revision(
        s.lease,
        revision=1,
        task_digest="d" * 64,
        parent_digest=D,
        manifest={"changed": "public condition"},
    )
    with pytest.raises(JournalConflict, match="current frozen revision"):
        s.journal.claim_operation(s.lease, s.key)
    revised = s.key.model_copy(update={"revision_digest": "d" * 64})
    assert s.journal.claim_operation(s.lease, revised).acquired
    assert s.journal.counts() == {"attempted_prs": 1, "revisions": 2, "selected_prs": 0}
    with pytest.raises(JournalConflict, match="digest, parent or manifest changed"):
        s.journal.register_revision(
            s.lease,
            revision=1,
            task_digest="e" * 64,
            parent_digest=D,
            manifest={"changed": "again"},
        )
    with pytest.raises(JournalConflict, match="current exact parent"):
        s.journal.register_revision(
            s.lease,
            revision=2,
            task_digest="e" * 64,
            parent_digest=D,
            manifest={"wrong_parent": True},
        )


def test_atomic_artifact_publication_is_immutable_and_hash_verified(setup):
    s = setup
    s.journal.claim_operation(s.lease, s.key)
    reference = s.journal.commit_artifact(
        s.lease, s.key.operation_id, name="output", data=b"immutable output"
    )
    assert s.journal.verify_artifact(reference).read_bytes() == b"immutable output"
    assert (
        s.journal.commit_artifact(
            s.lease, s.key.operation_id, name="output", data=b"immutable output"
        )
        == reference
    )
    with pytest.raises(JournalConflict, match="cannot change content"):
        s.journal.commit_artifact(
            s.lease, s.key.operation_id, name="output", data=b"different output"
        )
    s.journal.verify_artifact(reference).write_bytes(b"tampered output!")
    with pytest.raises(ArtifactCorrupt):
        s.journal.finish_operation(s.lease, s.key.operation_id, receipt={"done": True})
    assert s.journal.get_operation(s.key.operation_id).status == "claimed"


@pytest.mark.parametrize("crash_boundary", ["before_publish", "after_publish"])
def test_crash_during_artifact_commit_recovers_exact_bytes_without_rerunning_effect(
    setup, monkeypatch, crash_boundary
):
    s = setup
    s.journal.claim_operation(s.lease, s.key)
    original = s.journal._publish_blob

    def crash(reference, temporary):
        if crash_boundary == "after_publish":
            original(reference, temporary)
        raise KeyboardInterrupt

    monkeypatch.setattr(s.journal, "_publish_blob", crash)
    with pytest.raises(KeyboardInterrupt):
        s.journal.commit_artifact(
            s.lease, s.key.operation_id, name="answer", data=b"fully retained output"
        )
    restored = Journal(s.journal.path, clock=lambda: s.now[0])
    with pytest.raises(ArtifactCorrupt, match="pending artifact"):
        restored.finish_operation(s.lease, s.key.operation_id, receipt={"done": True})
    recovered = restored.recover_artifacts(s.lease)
    assert len(recovered) == 1
    assert restored.verify_artifact(recovered[0]).read_bytes() == b"fully retained output"
    finished = restored.finish_operation(s.lease, s.key.operation_id, receipt={"done": True})
    assert finished.artifacts == {"answer": recovered[0]}
    assert restored.recover_artifacts(s.lease) == []
    assert not restored.claim_operation(s.lease, s.key).acquired


def test_missing_or_linked_blob_cannot_be_reused(setup, tmp_path):
    s = setup
    s.journal.claim_operation(s.lease, s.key)
    reference = s.journal.commit_artifact(
        s.lease, s.key.operation_id, name="proof", data=b"evidence"
    )
    path = s.journal.verify_artifact(reference)
    path.unlink()
    with pytest.raises(ArtifactCorrupt, match="unavailable"):
        s.journal.verify_artifact(reference)
    other = tmp_path / "external"
    other.write_bytes(b"evidence")
    path.symlink_to(other)
    with pytest.raises(ArtifactCorrupt, match="symlinks"):
        s.journal.verify_artifact(reference)


def test_completed_checkpoint_reuse_rechecks_artifact_bytes(setup):
    s = setup
    s.journal.claim_operation(s.lease, s.key)
    reference = s.journal.commit_artifact(
        s.lease, s.key.operation_id, name="proof", data=b"original proof"
    )
    s.journal.finish_operation(s.lease, s.key.operation_id, receipt={"done": True})
    s.journal.verify_artifact(reference).unlink()
    with pytest.raises(ArtifactCorrupt, match="unavailable"):
        s.journal.claim_operation(s.lease, s.key)
    with pytest.raises(ArtifactCorrupt, match="unavailable"):
        s.journal.finish_operation(s.lease, s.key.operation_id, receipt={"done": True})


def admitted(s):
    s.journal.claim_operation(s.lease, s.key)
    reference = s.journal.commit_artifact(
        s.lease, s.key.operation_id, name="proof", data=b"full exact revision evidence"
    )
    s.journal.finish_operation(s.lease, s.key.operation_id, receipt={"artifact": reference.sha256})
    report = QualityReport(
        pr=s.pr,
        revision_digest=D,
        criteria={
            name: {
                "status": "pass",
                "score": 3,
                "explanation": "Required exact evidence was observed.",
                "evidence_ids": ["proof"],
            }
            for name in QUALITY_DIMENSIONS
        },
    )
    context = AdmissionContext(
        pr=s.pr,
        revision_digest=D,
        required_trials=[{"id": "oracle", "kind": "oracle", "expected_reward": 1}],
        trials=[
            {
                "id": "oracle",
                "revision_digest": D,
                "kind": "oracle",
                "outcome": "passed",
                "reward": 1,
                "environment_healthy": True,
                "collection_complete": True,
                "materialization_verified": True,
                "checks_run": 1,
                "evidence_ids": ["proof"],
                "diagnostic": "Protected check passed.",
            }
        ],
        evidence={"proof": {"revision_digest": D, "artifact": reference}},
        execution_profile_validated=True,
        artifact_integrity_verified=True,
    )
    return report, context


def test_one_selected_revision_per_pr_recovers_idempotently_without_count_inflation(setup):
    s = setup
    report, context = admitted(s)
    assert s.journal.select_revision(s.lease, report=report, context=context)
    assert not s.journal.select_revision(s.lease, report=report, context=context)
    assert s.journal.counts() == {"attempted_prs": 1, "revisions": 1, "selected_prs": 1}
    with pytest.raises(JournalConflict, match="already has a selected"):
        s.journal.register_revision(
            s.lease, revision=1, task_digest="d" * 64, parent_digest=D, manifest={"new": True}
        )
    changed = report.model_copy(update={"difficulty": "Rewritten report"})
    with pytest.raises(JournalConflict, match="admission receipts changed"):
        s.journal.select_revision(s.lease, report=changed, context=context)


def test_selected_receipt_cannot_skip_missing_trial_or_tampered_evidence(setup):
    s = setup
    report, context = admitted(s)
    with pytest.raises(JournalConflict, match="Missing required trial"):
        s.journal.select_revision(
            s.lease, report=report, context=context.model_copy(update={"trials": []})
        )
    reference = context.evidence["proof"].artifact
    s.journal.verify_artifact(reference).write_bytes(b"changed")
    with pytest.raises(ArtifactCorrupt):
        s.journal.select_revision(s.lease, report=report, context=context)
    assert s.journal.counts()["selected_prs"] == 0


def test_stable_ledger_reference_has_no_secondary_charge_tables(setup):
    s = setup
    s.journal.claim_operation(s.lease, s.key)
    record = s.journal.get_operation(s.key.operation_id).model_dump(mode="json")
    assert record["ledger"] == s.ledger.model_dump()
    assert record["deadline"]["expires_at"] == 2000
    assert not any("cost" in key or "charged" in key for key in record)
