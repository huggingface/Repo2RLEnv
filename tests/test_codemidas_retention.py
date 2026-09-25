"""Incomplete audits must not become evidence of a defective task."""

import json

import pytest

from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, inspect_bundle, write_bundle
from repo2rlenv.pipelines.recipes.codemidas.audit import AuditReview, validate_review
from repo2rlenv.pipelines.recipes.codemidas.retention import retain
from repo2rlenv.quality.labels import read_evaluation


@pytest.mark.parametrize(
    ("outcome", "exploit", "provider_blocked", "expected", "reason"),
    [
        ("legitimate_success", False, True, "blocked", "codemidas_solver_review_passed"),
        ("uncertain", False, True, "blocked", "codemidas_review_incomplete"),
        ("infrastructure", False, False, "blocked", "codemidas_review_incomplete"),
        ("false_positive", False, True, "needs_repair", "codemidas_audit_defect"),
        ("false_negative", False, False, "needs_repair", "codemidas_audit_defect"),
        ("legitimate_success", True, False, "needs_repair", "codemidas_audit_defect"),
    ],
)
def test_retention_distinguishes_defects_from_incomplete_evidence(
    tmp_path, monkeypatch, outcome, exploit, provider_blocked, expected, reason
):
    task = write_bundle(
        TaskBundle(
            name="example",
            org="test",
            instruction="Restore the requested public behavior.",
            metadata={
                "recipe": "codemidas",
                "recipe_version": "1",
                "reward_kinds": ["test_execution"],
                "quality_status": "exported",
            },
            files={
                "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
                "solution/solve.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
                "tests/test.sh": TaskFile.text("#!/bin/sh\nexit 1\n", executable=True),
            },
        ),
        tmp_path / "generated",
    )
    original = inspect_bundle(task)
    original_config = (task / "task.toml").read_bytes()
    monkeypatch.setattr(
        "repo2rlenv.pipelines.recipes.codemidas.audit.control_evidence", lambda *args: []
    )
    receipt = tmp_path / "audit.json"
    receipt.write_text(
        json.dumps(
            {
                "bundle_hash": original["bundle_hash"],
                "screen_rewards": [],
                "curriculum": "incomplete",
                "method_sound": False,
                "solver_review_sound": outcome == "legitimate_success",
                "adversarial_status": "blocked" if provider_blocked else "completed",
                "review": {
                    "exploit_confirmed": exploit,
                    "judgments": [{"trial": f"solve-{i}", "outcome": outcome} for i in range(4)],
                },
            }
        )
    )
    retained = retain(task, tmp_path / "retained", tmp_path / "controls", audit=receipt)
    label = read_evaluation(retained)
    assert label.status == expected
    assert reason in label.reason_codes
    assert inspect_bundle(retained) == original
    assert (task / "task.toml").read_bytes() == original_config
    assert label.evidence[-1].path == str(receipt.resolve())


def test_blocked_probe_cannot_establish_a_confirmed_exploit():
    review = AuditReview(
        sound=False,
        solver_sound=True,
        exploit_confirmed=True,
        exploit_explanation="A purported exploit without a completed adversarial attempt.",
        judgments=[
            {
                "trial": f"solve-{i}",
                "outcome": "legitimate_success",
                "explanation": "The implementation satisfies its public behavioral contract.",
                "evidence": [f"attempts/solve-{i}/result.json"],
            }
            for i in range(4)
        ],
        issues=["Unsupported exploit claim"],
    )
    with pytest.raises(ValueError, match="cannot confirm an exploit"):
        validate_review(review, {}, [{"trial": "exploit", "exception": "ProviderPolicyBlocked"}])
