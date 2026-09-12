from __future__ import annotations

import pytest
from pydantic import ValidationError

from repo2rlenv.quality.report import REQUIRED_CHECKS, CheckResult, Evidence, QualityReport


def report():
    evidence = Evidence(path="receipt.json", sha256="1" * 64)
    return QualityReport(
        bundle_hash="sha256:" + "2" * 64,
        recipe="fixture",
        recipe_version="1",
        checks={
            key: CheckResult(status="pass", explanation="Verified", evidence=[evidence])
            for key in REQUIRED_CHECKS
        },
    )


def test_failure_cannot_be_averaged_away():
    value = report()
    assert value.accepted
    value.scores.clarity = value.scores.coverage = value.scores.robustness = 4
    value.checks["isolation"].status = "fail"
    assert not value.accepted


def test_unknown_and_missing_checks_are_not_passes():
    value = report()
    value.checks["blind_opus"].status = "unknown"
    assert not value.accepted
    del value.checks["blind_opus"]
    assert not value.accepted
    with pytest.raises(ValidationError, match="artifact evidence"):
        CheckResult(status="pass", explanation="Unsubstantiated")


def test_unsolved_task_can_still_be_valid_and_stale_report_is_detected():
    value = report()
    value.solver_success = {"sonnet": False, "opus": False}
    assert value.accepted
    assert not value.current_for("sha256:" + "3" * 64)


def test_only_reasoning_regression_check_may_be_inapplicable():
    value = report()
    value.checks["regression"] = CheckResult(
        status="not_applicable", explanation="No pre-existing repository tests"
    )
    assert not value.accepted
    value.profile = "reasoning"
    assert value.accepted
