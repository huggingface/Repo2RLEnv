"""Feedback is bound to saved evidence and never silently changes acceptance."""

import json

import pytest

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.quality.feedback import (
    Adjudication,
    adjudicate_feedback,
    load_feedback,
    record_feedback,
)
from repo2rlenv.quality.loop.context import EvidenceContext
from repo2rlenv.quality.loop.models import LoopOptions
from repo2rlenv.quality.reviewer.profile import identity, save_review
from tests import test_quality_reviewer as fixtures
from tests.test_quality_reviewer import decision

task = fixtures.task


def feedback(task, tmp_path):
    review = decision(fail=True)
    root = tmp_path / "review"
    record = save_review(
        root,
        "r0",
        "sha256:" + "0" * 64,
        review,
        EvidenceContext(task, [], limit=16000),
        [],
        identity(LoopOptions(profile="evidence-review-v2")),
    )
    return record_feedback(
        root,
        issue_id=record["findings"][0]["issue_id"],
        author="maintainer",
        decision="disputed",
        reason="Check the arithmetic coverage carefully",
        directory=tmp_path / "feedback",
    )


def test_feedback_tampering_is_detected(task, tmp_path):
    path = feedback(task, tmp_path)
    assert load_feedback(path)["feedback"]["author"] == "maintainer"
    raw = json.loads(path.read_text())
    raw["evidence"]["documents"]["instruction.md"] = "Tampered"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="unchanged review"):
        load_feedback(path)


class Judge:
    def __init__(self, quote="sum of two integers"):
        self.quote = quote

    def ask(self, schema, model, system, user, key):
        assert "not ground truth" in system
        return Adjudication(
            decision="unresolved",
            explanation="Insufficient implementation evidence",
            evidence=[dict(path="instruction.md", quote=self.quote)],
            clean_regression="A verifier that checks negative operands",
            defective_regression="A verifier that omits negatives",
        )


def test_adjudication_is_a_second_opinion_not_override(task, tmp_path):
    path = feedback(task, tmp_path)
    original = path.read_bytes()
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="2")
    result = adjudicate_feedback(
        path,
        directory=tmp_path / "adjudication",
        ledger=ledger,
        options=LoopOptions(),
        model_client=Judge(),
    )
    assert result["label_changed"] is False
    assert result["regression_status"] == "suggested_not_audited"
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match="identical"):
        adjudicate_feedback(
            path,
            directory=tmp_path / "adjudication",
            ledger=ledger,
            options=LoopOptions(),
            model_client=Judge(),
        )
    with pytest.raises(ValueError, match="frozen"):
        adjudicate_feedback(
            path,
            directory=tmp_path / "bad-adjudication",
            ledger=ledger,
            options=LoopOptions(),
            model_client=Judge("invented"),
        )
