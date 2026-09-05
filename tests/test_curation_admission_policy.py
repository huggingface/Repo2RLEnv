from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from repo2rlenv.curation.inference import inference_digest
from repo2rlenv.curation.models import (
    CRITERIA,
    CampaignConfig,
    Review,
    TrialEvidence,
    acceptance,
    quality_gate_reasons,
    review_scores,
    validate_review_scores,
)


@pytest.fixture
def easy_review():
    result = Review.model_validate(
        {
            "criteria": {
                name: {
                    "score": 4,
                    "outcome": "pass",
                    "explanation": "Independent evidence supports this criterion.",
                    "evidence": ["task/tests/test_contract.py"],
                }
                for name in CRITERIA
            },
            "blockers": [],
            "failure_attribution": {"solver-0-0": "reasoning", "solver-1-0": "solved"},
            "reward_hacks": [],
            "suggested_repairs": [],
            "adversary_assessment": "attempted_hack",
        }
    )
    result.criteria["intrinsic_difficulty"].score = 0
    result.criteria["intrinsic_difficulty"].outcome = "fail"
    return result


def passing_trials(config):
    expected = {
        "baseline": 0,
        "tamper": 0,
        "pytest-tamper": 0,
        "mutation-wrong": 0,
        "equivalent-alternative": 1,
        **{f"oracle-{i}": 1 for i in range(config.oracle_repeats)},
    }
    trials = [
        TrialEvidence(label=label, reward=reward, task_digest="digest", path="trial")
        for label, reward in expected.items()
    ]
    for i, model in enumerate(config.solver_models):
        trials.append(
            TrialEvidence(
                label=f"solver-{i}-0",
                reward=i,
                task_digest="digest",
                path="trial",
                model=model,
                inference_digest=inference_digest(model),
            )
        )
    trials.append(
        TrialEvidence(
            label="adversary",
            reward=0,
            task_digest="digest",
            path="trial",
            model=config.author_model,
            inference_digest=inference_digest(config.author_model, adversary=True),
        )
    )
    return trials


def test_omitted_policy_keeps_legacy_difficulty_gate(easy_review):
    config = CampaignConfig.model_validate({"acceptance_score": 85})
    assert config.acceptance_policy == "legacy"
    assert easy_review.score == 90
    assert quality_gate_reasons(easy_review, config) == [
        "Criterion not passed: intrinsic_difficulty"
    ]
    with pytest.raises(ValidationError):
        CampaignConfig(acceptance_policy="ignore_difficulty")


@pytest.mark.parametrize("outcome", ["fail", "not_applicable", "pass"])
def test_validity_admits_easy_task_only_after_all_other_gates(easy_review, outcome):
    config = CampaignConfig(acceptance_policy="validity")
    easy_review.criteria["intrinsic_difficulty"].outcome = outcome
    assert (
        acceptance(
            passing_trials(config), easy_review, config, "digest", ["wrong"], ["alternative"]
        )
        == []
    )
    assert easy_review.validity_score == 100
    assert easy_review.score == 90


def test_all_solvers_succeeding_does_not_invalidate_an_easy_task(easy_review):
    config = CampaignConfig(acceptance_policy="validity")
    trials = passing_trials(config)
    for trial in trials:
        if trial.label.startswith("solver-"):
            trial.reward = 1
            easy_review.failure_attribution[trial.label] = "solved"
    assert acceptance(trials, easy_review, config, "digest", ["wrong"], ["alternative"]) == []


def test_validity_score_normalizes_weights_and_applies_configured_threshold(easy_review):
    easy_review.criteria["test_coverage"].score = 3
    # Seven criteria have90 total weight; coverage loses5 points, giving85/90.
    assert easy_review.validity_score == pytest.approx(100 * 85 / 90)
    assert easy_review.score == 85
    config = CampaignConfig(acceptance_policy="validity", acceptance_score=95)
    assert quality_gate_reasons(easy_review, config) == ["Validity score 94.4 below 95.0"]
    assert not quality_gate_reasons(
        easy_review, CampaignConfig(acceptance_policy="validity", acceptance_score=94)
    )


@pytest.mark.parametrize("name", [n for n in CRITERIA if n != "intrinsic_difficulty"])
@pytest.mark.parametrize("score,outcome", [(2, "pass"), (4, "fail"), (4, "not_applicable")])
def test_every_other_criterion_gate_remains(easy_review, name, score, outcome):
    criterion = easy_review.criteria[name]
    criterion.score, criterion.outcome = score, outcome
    reasons = quality_gate_reasons(easy_review, CampaignConfig(acceptance_policy="validity"))
    assert f"Criterion not passed: {name}" in reasons


def test_validity_keeps_blockers_hacks_and_incomplete_evidence(easy_review):
    config = CampaignConfig(acceptance_policy="validity")
    easy_review.blockers = ["Explicit unresolved blocker"]
    easy_review.reward_hacks = ["Observed successful verifier bypass"]
    easy_review.adversary_assessment = "unknown"
    reasons = acceptance(
        passing_trials(config)[1:], easy_review, config, "digest", ["wrong"], ["alternative"]
    )
    assert "Explicit unresolved blocker" in reasons
    assert "Reward hack: Observed successful verifier bypass" in reasons
    assert "Incomplete adversarial audit: unknown" in reasons
    assert any(reason.startswith("baseline: missing") for reason in reasons)


def test_difficulty_still_requires_a_complete_evidence_backed_review(easy_review):
    payload = easy_review.model_dump()
    del payload["criteria"]["intrinsic_difficulty"]
    with pytest.raises(ValidationError, match="every criterion"):
        Review.model_validate(payload)


def test_score_receipts_report_both_scales_without_changing_review_serialization(easy_review):
    legacy = review_scores(easy_review, CampaignConfig())
    validity = review_scores(easy_review, CampaignConfig(acceptance_policy="validity"))
    assert legacy == {
        "score": 90,
        "legacy_score": 90,
        "validity_score": 100,
        "intrinsic_difficulty_score": 0,
        "acceptance_policy": "legacy",
    }
    assert validity == {**legacy, "score": 100, "acceptance_policy": "validity"}
    assert "score" not in easy_review.model_dump()
    assert Review.model_validate_json(easy_review.model_dump_json()).score == 90
    with pytest.raises(ValueError, match="Unknown acceptance policy"):
        easy_review.admission_score("other")


def test_legacy_receipts_remain_readable_but_cannot_be_reclassified(easy_review):
    validate_review_scores({"score": 90}, easy_review, CampaignConfig())
    with pytest.raises(ValueError, match="acceptance policy mismatch"):
        validate_review_scores(
            {"score": 90}, easy_review, CampaignConfig(acceptance_policy="validity")
        )


@pytest.mark.parametrize(
    "field",
    ["score", "legacy_score", "validity_score", "intrinsic_difficulty_score", "acceptance_policy"],
)
def test_validity_receipts_require_all_correct_values(easy_review, field):
    config = CampaignConfig(acceptance_policy="validity")
    receipt = review_scores(easy_review, config)
    validate_review_scores(receipt, easy_review, config)
    del receipt[field]
    with pytest.raises(ValueError, match="mismatch"):
        validate_review_scores(receipt, easy_review, config)


@pytest.mark.parametrize("value", [99, True, float("nan"), "100"])
def test_tampered_receipt_scores_fail(easy_review, value):
    config = CampaignConfig(acceptance_policy="validity")
    receipt = {**review_scores(easy_review, config), "validity_score": value}
    with pytest.raises(ValueError, match="validity_score mismatch"):
        validate_review_scores(receipt, easy_review, config)


def retained_receipt(root, review, *, comparison):
    from repo2rlenv.curation.artifacts import digest_task
    from repo2rlenv.curation.campaign import ADMISSION_VERSION

    config = CampaignConfig(acceptance_policy="validity")
    identity = "example-project-1"
    runtime = "pi"
    candidate = root / "candidates"
    if comparison:
        candidate /= runtime
    task = candidate / identity / "123" / "revision-0" / "task"
    task.mkdir(parents=True)
    (task / "payload.txt").write_text("same immutable task content")
    (task / "contract.json").write_text(
        json.dumps(
            {
                "title": "A substantive behavior change",
                "rationale": "Observe independent results",
                "source_paths": ["src/project"],
                "min_tests": 3,
                "requirements": [
                    {"id": "r1", "behavior": "Returns exact values", "tests": ["a", "b"]},
                    {"id": "r2", "behavior": "Handles alternatives", "tests": ["c"]},
                ],
                "mutations": [
                    {"name": name, "rationale": "Break observable behavior", "script": "false"}
                    for name in ["zero", "wrong"]
                ],
                "equivalents": [
                    {
                        "name": "alternative",
                        "rationale": "Preserve the behavior",
                        "script": "true",
                    }
                ],
            }
        )
    )
    digest = digest_task(task)
    trials = [trial.model_copy(update={"task_digest": digest}) for trial in passing_trials(config)]
    trials.append(TrialEvidence(label="mutation-zero", task_digest=digest, reward=0, path="trial"))
    (task.parent / "evidence.json").write_text(
        json.dumps(
            {
                "admission_version": ADMISSION_VERSION,
                "task_digest": digest,
                "trials": [trial.model_dump() for trial in trials],
            }
        )
    )
    review_path = task.parent / "review.json"
    review_path.write_text(review.model_dump_json())
    released = root / "tasks"
    if comparison:
        released /= runtime
    shutil.copytree(task, released / identity)
    row = {
        "id": identity,
        "status": "accepted",
        "task_path": str(task),
        "task_digest": digest_task(task),
        "review_path": str(review_path),
        "admission_version": ADMISSION_VERSION,
        **review_scores(review, config),
    }
    if comparison:
        row["runtime"] = runtime
    manifest = {"rows" if comparison else "accepted": [row]}
    manifest_path = root / ("comparison.json" if comparison else "manifest.json")
    manifest_path.write_text(json.dumps(manifest))
    (root / "config.json").write_text(config.model_dump_json())
    return row, config, manifest, manifest_path


@pytest.fixture
def score_receipt_validation(monkeypatch):
    from repo2rlenv.curation import publish

    # These tests isolate score policy and snapshot path resolution. Full proof
    # validation is covered by the receipt and publication integration suites.
    # Read the supplied folder each time so live-review changes and blockers
    # still exercise the actual score and acceptance checks below this gate.
    def retained_review(folder, task, trials, *, model, acceptance_policy):
        return Review.model_validate_json((folder / "review.json").read_text())

    monkeypatch.setattr(publish, "validate_review_receipt", retained_review)


@pytest.mark.parametrize("comparison", [False, True])
def test_publication_validates_receipt_from_snapshot_not_live_review(
    tmp_path, easy_review, comparison, score_receipt_validation
):
    from repo2rlenv.curation.publish import _validate_admissions, evidence_snapshot

    row, _, _, _ = retained_receipt(tmp_path, easy_review, comparison=comparison)
    with evidence_snapshot(tmp_path) as snapshot:
        # Absolute row paths refer to the original run; inspect their frozen
        # snapshot counterparts even if original evidence subsequently changes.
        easy_review.criteria["test_coverage"].score = 0
        Path(row["review_path"]).write_text(easy_review.model_dump_json())
        _validate_admissions(snapshot, origin_root=tmp_path)


@pytest.mark.parametrize("comparison", [False, True])
@pytest.mark.parametrize("damage", ["missing_policy", "score", "missing_scale", "config"])
def test_publication_rejects_changed_or_missing_validity_receipts(
    tmp_path, easy_review, comparison, damage, score_receipt_validation
):
    from repo2rlenv.curation.publish import _validate_admissions

    row, _, manifest, manifest_path = retained_receipt(tmp_path, easy_review, comparison=comparison)
    if damage == "missing_policy":
        del row["acceptance_policy"]
    elif damage == "score":
        row["score"] = 99
    elif damage == "missing_scale":
        del row["legacy_score"]
    else:
        (tmp_path / "config.json").write_text(CampaignConfig().model_dump_json())
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="mismatch"):
        _validate_admissions(tmp_path)


def test_comparison_recovery_rejects_legacy_reclassification_and_receipt_tampering(
    tmp_path, easy_review
):
    from repo2rlenv.curation.compare import _check_accepted

    row, config, _, _ = retained_receipt(tmp_path, easy_review, comparison=True)
    source = {"id": row["id"]}
    _check_accepted(row, tmp_path, source, released=True, config=config)
    _check_accepted(row, tmp_path, source, released=False, config=config)
    legacy = {k: v for k, v in row.items() if k != "acceptance_policy"}
    with pytest.raises(ValueError, match="acceptance policy mismatch"):
        _check_accepted(legacy, tmp_path, source, released=True, config=config)
    with pytest.raises(ValueError, match="validity_score mismatch"):
        _check_accepted(
            {**row, "validity_score": 99}, tmp_path, source, released=True, config=config
        )
