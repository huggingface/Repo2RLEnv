from __future__ import annotations

import json

import pytest

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.quality.loop.context import EvidenceContext
from repo2rlenv.quality.loop.models import Assessment, Citation, LoopOptions, Review
from repo2rlenv.quality.loop.protocol import citation_path_error
from repo2rlenv.quality.loop.runner import QualityLoop


def decision(citation):
    assessment = Assessment(
        status="pass", score=3, explanation="Check state restoration.", evidence=[citation]
    )
    return Review(
        summary="Review the context-manager contract.",
        task=assessment.model_copy(deep=True),
        verifier=assessment.model_copy(deep=True),
        leakage=assessment.model_copy(deep=True),
        rollout="not_run",
        issues=[],
        probes=[],
        read_requests=[],
    )


@pytest.fixture
def context(tmp_path):
    (tmp_path / "instruction.md").write_text("Restore the original state.\n")
    return EvidenceContext(tmp_path, [], limit=16000)


@pytest.mark.parametrize(
    "quote,other_text",
    [
        ('"status": "needs_repair"', 'status = "needs_repair"'),
        ('"status": "needs_repair"', 'status = "generated"'),
        ("expected 1.0", "expected 1.1"),
        ("Restore the original State.", "Restore the original state."),
        ("assert False is True\nAssertionError", "assert False is True\nE AssertionError"),
    ],
)
def test_absent_citations_report_exact_source_miss_without_inference(context, quote, other_text):
    context.documents["other.txt"] = other_text
    value = decision(Citation(path="evidence/task-context.json", quote=quote))
    before = value.model_dump()
    with pytest.raises(ValueError) as error:
        context.validate_review(value)
    feedback = str(error.value)
    assert "evidence/task-context.json" in feedback
    assert f"invalid quote={quote!r}" in feedback
    assert "Exact quote matches 0 supplied document(s): []" in feedback
    assert value.model_dump() == before


def test_ambiguous_path_feedback_lists_bounded_exact_sources(context):
    citation = Citation(path="wrong.json", quote="a specific unchanged claim")
    context.documents.update({f"source-{index:02}.txt": citation.quote for index in range(12)})
    with pytest.raises(ValueError) as error:
        context.validate_review(decision(citation))
    feedback = str(error.value)
    assert "wrong.json" in feedback
    assert "Exact quote matches 12 supplied document(s)" in feedback
    assert all(f"source-{index:02}.txt" in feedback for index in range(8))
    assert "source-08.txt" not in feedback
    assert "4 additional paths omitted" in feedback
    assert citation.path == "wrong.json"


def test_unique_wrong_path_is_reported_without_automatic_relocation(context):
    citation = Citation(path="wrong.md", quote="Restore the original state.")
    with pytest.raises(ValueError) as error:
        context.validate_review(decision(citation))
    assert "Exact quote matches 1 supplied document(s): ['instruction.md']" in str(error.value)
    assert citation.path == "wrong.md"
    corrected = citation.model_copy(update={"path": "instruction.md"})
    context.validate_review(decision(corrected))


def test_supplied_matching_path_remains_valid_with_duplicate_text(context):
    context.documents["duplicate.md"] = "Restore the original state."
    context.validate_review(
        decision(Citation(path="instruction.md", quote="Restore the original state."))
    )


def test_feedback_bounds_the_repeated_quote(context):
    citation = Citation(path="wrong.md", quote="x" * 1000)
    feedback = citation_path_error(citation, context.documents)
    assert repr("x" * 250) in feedback
    assert "x" * 251 not in feedback


def test_precise_citation_feedback_uses_existing_review_correction(tmp_path):
    pytest.importorskip("harbor")
    task = write_bundle(
        TaskBundle(
            name="citation-review",
            org="tests",
            instruction="Restore the original state.\n",
            files={
                "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
                "solution/solve.sh": TaskFile.text("#!/bin/sh\ntrue\n", executable=True),
                "tests/test.sh": TaskFile.text("#!/bin/sh\ntrue\n", executable=True),
            },
            metadata={"recipe": "test", "recipe_version": "1", "reward_kinds": ["test_execution"]},
        ),
        tmp_path / "tasks",
    )

    class Model:
        calls = 0

        def ask(self, schema, model, system, user, key):
            self.calls += 1
            if self.calls == 1:
                return decision(
                    Citation(path="evidence/task-context.json", quote='"status": "needs_repair"')
                )
            feedback = json.loads(user)["protocol_feedback"][-1]
            assert "evidence/task-context.json" in feedback
            assert "Exact quote matches 0 supplied document(s): []" in feedback
            return decision(Citation(path="instruction.md", quote="Restore the original state."))

    model = Model()
    loop = QualityLoop(
        LoopOptions(max_read_rounds=1),
        tmp_path / "quality",
        BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="1"),
        model_client=model,
    )
    review, _ = loop._review(task, [], "citation", probe_limit=0)
    assert review.sound
    assert model.calls == 2
    assert review.task.evidence[0].path == "instruction.md"
