from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from repo2rlenv.tasksmith.models import (
    QUALITY_DIMENSIONS,
    AdmissionContext,
    Deadline,
    MaterializationContract,
    OperationKey,
    PRIdentity,
    QualityCriterion,
    QualityReport,
    TaskBundle,
    TaskContract,
    TrialRecord,
    admission_reasons,
    digest,
)

D = "a" * 64


def task_data():
    return {
        "pr": {"repository": "Example/Library", "number": 12},
        "source": {
            "before_commit": "a" * 40,
            "reference_commit": "b" * 40,
            "patch_digest": "c" * 64,
            "captured_at": 1000,
        },
        "revision": 0,
        "useful_outcome": "Preserve the useful public outcome of the PR.",
        "requirements": [
            {
                "id": "output",
                "statement": "Return the input doubled.",
                "public_evidence": ["instruction.md:1"],
            }
        ],
        "editable_paths": ["src/library"],
        "collection": [{"source": "src/library", "destination": "src/library"}],
        "exposure": [
            {"path": "src", "visibility": "solver"},
            {"path": "tests", "visibility": "grader"},
        ],
    }


def reports():
    pr = PRIdentity(repository="example/library", number=12)
    report = QualityReport(
        pr=pr,
        revision_digest=D,
        criteria={
            name: {
                "status": "pass",
                "score": 3,
                "explanation": "Supported by exact-revision evidence.",
                "evidence_ids": ["proof"],
            }
            for name in QUALITY_DIMENSIONS
        },
    )
    context = AdmissionContext(
        pr=pr,
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
                "diagnostic": "Protected behavior check passed.",
            }
        ],
        evidence={
            "proof": {
                "revision_digest": D,
                "artifact": {"sha256": D, "size_bytes": 10, "path": "receipt.json"},
            }
        },
        execution_profile_validated=True,
        artifact_integrity_verified=True,
    )
    return report, context


def test_pr_identity_is_canonical_and_source_url_is_not_ambiguous():
    assert (
        PRIdentity.from_url("https://github.com/EXAMPLE/Library/pull/012").key
        == "github.com/example/library/pull/12"
    )
    assert (
        PRIdentity(repository="example/library", number=12).url
        == "https://github.com/example/library/pull/12"
    )
    for url in [
        "http://github.com/example/library/pull/12",
        "https://github.com.evil/example/library/pull/12",
        "https://github.com/example/library/pull/12/files",
        "https://github.com/example/library/pull/12?other=1",
    ]:
        with pytest.raises(ValueError):
            PRIdentity.from_url(url)


def test_one_observable_requirement_is_allowed_and_unknown_fields_fail_closed():
    task = TaskContract.model_validate(task_data())
    assert len(task.requirements) == 1
    assert TaskContract.model_validate_json(task.model_dump_json()) == task
    assert digest(task) == digest(task.model_dump(mode="json"))
    with pytest.raises(ValidationError, match="extra_forbidden"):
        TaskContract.model_validate(task_data() | {"skip_validation": True})


@pytest.mark.parametrize(
    "path", ["/tmp/source", "../source", "src/../escape", "src//library", "src\\library", "."]
)
def test_unsafe_export_paths_are_rejected(path):
    data = task_data()
    data["collection"][0]["destination"] = path
    with pytest.raises(ValidationError, match="canonical relative path"):
        TaskContract.model_validate(data)


def test_full_edit_boundary_including_new_helpers_must_be_collected():
    data = task_data()
    data["collection"][0]["source"] = "src/library/existing.py"
    with pytest.raises(ValidationError, match="not completely collected"):
        TaskContract.model_validate(data)
    data = task_data()
    data["collection"].append({"source": "helper", "destination": "src/library/helper"})
    with pytest.raises(ValidationError, match="Overlapping paths"):
        TaskContract.model_validate(data)
    data = task_data()
    data["exposure"].append({"path": "src/private_answer", "visibility": "oracle"})
    with pytest.raises(ValidationError, match="Overlapping paths"):
        TaskContract.model_validate(data)


def test_revision_zero_and_parent_are_bound():
    data = task_data()
    data["revision"] = 1
    with pytest.raises(ValidationError, match="parent digest"):
        TaskContract.model_validate(data)
    data["parent_digest"] = D
    assert TaskContract.model_validate(data).revision == 1


def bundle_data():
    task = TaskContract.model_validate(task_data())
    materialization = MaterializationContract(
        collection=task.collection,
        mode="source",
        clean_destination="/submission",
        origin_checks=["Module path is under /submission."],
    )
    return {
        "task": task.model_dump(),
        "episode": {
            "initial_source": "a" * 40,
            "reset": "Fresh immutable source snapshot.",
            "observations": ["shell stdout"],
            "tools": ["shell"],
            "cpus": 2,
            "memory_mib": 2048,
            "seed_policy": "Fixed retained inputs.",
            "horizon_seconds": 100,
            "termination": "Terminal protected reward.",
        },
        "verifier": {
            "requirement_checks": {"output": ["double"]},
            "expected_values": {
                "double": {
                    "provenance": "public_math",
                    "explanation": "Twice seven is fourteen.",
                    "evidence_ids": ["fixture"],
                }
            },
            "cases": {"double": ["Input seven, expected fourteen."]},
            "protected_runner": "trusted-probe-v1",
            "tolerance_policy": "Exact integer equality.",
            "control_ids": ["wrong_constant"],
        },
        "oracle": {
            "source_commit": "b" * 40,
            "patch": {"sha256": "c" * 64, "size_bytes": 10, "path": "oracle.patch"},
            "preconditions": ["Initial source matches before commit."],
            "requirement_ids": ["output"],
            "materialization_digest": digest(materialization),
        },
        "materialization": materialization.model_dump(),
    }


@pytest.mark.parametrize(
    "change",
    [
        "hidden_requirement",
        "wrong_seed",
        "wrong_reference",
        "wrong_materialization",
        "empty_checks",
    ],
)
def test_sections_cannot_silently_disagree(change):
    data = bundle_data()
    assert TaskBundle.model_validate(data)
    if change == "hidden_requirement":
        data["oracle"]["requirement_ids"] = ["private_layout"]
    elif change == "wrong_seed":
        data["episode"]["initial_source"] = "d" * 40
    elif change == "wrong_reference":
        data["oracle"]["source_commit"] = "d" * 40
    elif change == "wrong_materialization":
        data["oracle"]["materialization_digest"] = "e" * 64
    else:
        data["verifier"]["cases"]["double"] = []
    with pytest.raises(ValidationError):
        TaskBundle.model_validate(data)


def test_binary_reward_distinguishes_invalid_submission_from_infrastructure():
    _, context = reports()
    trial = context.trials[0].model_dump()
    trial.update(
        outcome="submission_failure",
        reward=0,
        checks_run=0,
        diagnostic="Correctly collected invalid source failed the healthy pinned compiler.",
    )
    assert TrialRecord.model_validate(trial).valid
    for missing in ["environment_healthy", "collection_complete", "materialization_verified"]:
        with pytest.raises(ValidationError, match="behavioral reward requires"):
            TrialRecord.model_validate(trial | {missing: False})
    with pytest.raises(ValidationError, match="cannot be a training reward"):
        TrialRecord.model_validate(trial | {"outcome": "infrastructure_failure"})
    assert not TrialRecord.model_validate(
        trial | {"outcome": "infrastructure_failure", "reward": None}
    ).valid
    with pytest.raises(ValidationError, match="Zero executed checks"):
        TrialRecord.model_validate(trial | {"outcome": "passed", "reward": 1})


@pytest.mark.parametrize("score", [None, 0, 1, 2, True])
def test_passing_status_cannot_contradict_score(score):
    with pytest.raises(ValidationError):
        QualityCriterion(
            status="pass", score=score, explanation="Supposed pass.", evidence_ids=["proof"]
        )


def test_high_other_scores_and_described_difficulty_do_not_override_a_failed_dimension():
    report, context = reports()
    assert admission_reasons(report, context) == []
    report.difficulty = "Very easy; both ordinary solvers solved it."
    assert admission_reasons(report, context) == []
    for criterion in report.criteria.values():
        criterion.score = 4
    report.criteria["oracle_validity"] = QualityCriterion(
        status="fail",
        score=0,
        explanation="Reference conflicts with public contract.",
        evidence_ids=["proof"],
        severity="material",
        proposed_repair="Reconcile and rerun the reference.",
    )
    assert any("oracle_validity" in reason for reason in admission_reasons(report, context))
    with pytest.raises(ValidationError, match="no material finding"):
        QualityCriterion(
            status="pass",
            score=4,
            explanation="Still broken.",
            evidence_ids=["proof"],
            severity="material",
            proposed_repair="Required correction.",
        )


def test_not_applicable_requires_explicit_controller_policy_and_evidence():
    report, context = reports()
    report.criteria["trajectory_findings"] = QualityCriterion(
        status="not_applicable",
        score=None,
        explanation="A justified controller-permitted exclusion.",
        evidence_ids=["proof"],
    )
    assert any("does not permit" in row for row in admission_reasons(report, context))
    context.allowed_not_applicable["trajectory_findings"] = (
        "Explicit bounded policy exception with separate execution evidence."
    )
    assert admission_reasons(report, context) == []
    context.trials = []
    assert any("Missing required trial" in row for row in admission_reasons(report, context))


@pytest.mark.parametrize(
    "change",
    [
        "report_revision",
        "trial_revision",
        "evidence_revision",
        "missing_receipt",
        "profile",
        "integrity",
        "required_trial",
        "wrong_reward",
    ],
)
def test_admission_is_exact_revision_and_requires_every_receipt(change):
    report, context = reports()
    if change == "report_revision":
        report.revision_digest = "b" * 64
    elif change == "trial_revision":
        context.trials[0].revision_digest = "b" * 64
    elif change == "evidence_revision":
        context.evidence["proof"].revision_digest = "b" * 64
    elif change == "missing_receipt":
        context.evidence = {}
    elif change == "profile":
        context.execution_profile_validated = False
    elif change == "integrity":
        context.artifact_integrity_verified = False
    elif change == "required_trial":
        context.trials = []
    else:
        context.trials = [
            TrialRecord.model_validate(
                context.trials[0].model_dump() | {"outcome": "submission_failure", "reward": 0}
            )
        ]
    assert admission_reasons(report, context)


def test_duplicate_trial_ids_do_not_inflate_required_counts():
    _, context = reports()
    data = context.model_dump()
    data["trials"].append(copy.deepcopy(data["trials"][0]))
    with pytest.raises(ValidationError, match="Trial IDs must be unique"):
        AdmissionContext.model_validate(data)


def test_stage_effect_identity_binds_all_inputs_and_original_deadline():
    key = OperationKey(
        pr_id="github.com/example/library/pull/12",
        revision_digest=D,
        stage="review",
        input_digest="b" * 64,
        policy_digest="c" * 64,
        attempt=0,
        effect="judge",
    )
    retry = key.model_copy(update={"attempt": 1})
    assert key.operation_id != retry.operation_id
    assert key.effect_id == retry.effect_id
    for field in ["revision_digest", "input_digest", "policy_digest"]:
        assert key.effect_id != key.model_copy(update={field: "f" * 64}).effect_id
    deadline = Deadline(expires_at=1000)
    assert deadline.require_remaining(now=990) == 10
    with pytest.raises(TimeoutError):
        deadline.require_remaining(now=1001)
