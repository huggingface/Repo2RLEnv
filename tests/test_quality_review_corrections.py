"""Bounded review corrections reuse drafts without trusting ungrounded claims."""

from __future__ import annotations

import json

import pytest

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.quality.loop.models import Assessment, Citation, LoopOptions, ReadRequest, Review
from repo2rlenv.quality.loop.runner import QualityLoop
from repo2rlenv.spec.input import LLMSpec


@pytest.fixture
def task(tmp_path):
    return write_bundle(
        TaskBundle(
            name="review-correction",
            org="tests",
            instruction="Restore the sum of two integers.\n",
            metadata={"recipe": "test", "recipe_version": "1", "reward_kinds": ["test_execution"]},
            files={
                "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
                "tests/test.sh": TaskFile.text(
                    "#!/bin/sh\n# independent sum check\n", executable=True
                ),
                "solution/solve.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
            },
        ),
        tmp_path / "tasks",
    )


def decision(summary, *, quote="sum of two integers", read=False):
    assessment = Assessment(
        status="pass",
        score=3,
        explanation="A useful arithmetic task.",
        evidence=[Citation(path="instruction.md", quote=quote)],
    )
    return Review(
        summary=summary,
        task=assessment,
        verifier=assessment.model_copy(deep=True),
        leakage=assessment.model_copy(deep=True),
        rollout="not_run",
        issues=[],
        probes=[],
        read_requests=[ReadRequest(path="tests/test.sh", query=None, start_line=1, end_line=2)]
        if read
        else [],
    )


class ScriptedModel:
    def __init__(self, replies):
        self.replies, self.calls = iter(replies), []

    def ask(self, schema, model, system, user, key):
        assert schema is Review
        self.calls.append({"key": key, "model": model, "payload": json.loads(user)})
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply.model_copy(deep=True)


def make_loop(tmp_path, model, **options):
    return QualityLoop(
        LoopOptions(**options),
        tmp_path / "quality",
        BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="10"),
        model_client=model,
    )


def test_citation_correction_receives_rejected_review_and_grounding_feedback(task, tmp_path):
    rejected = decision("A completed draft with one ungrounded quote", quote="invented outcome")
    corrected = decision("The same assessment with a grounded citation")
    model = ScriptedModel([rejected, corrected])
    loop = make_loop(tmp_path, model, max_read_rounds=1)
    accepted, context = loop._review(task, [], "before", probe_limit=0)
    first, correction = [call["payload"] for call in model.calls]
    assert first["previous_review"] is None
    assert correction["previous_review"] == rejected.model_dump()
    assert "not grounded" in correction["protocol_feedback"][-1]
    assert not any("invented outcome" in text for text in correction["documents"].values())
    context.validate_review(accepted)
    assert accepted == corrected
    assert [call["key"] for call in model.calls] == ["before-0", "before-1"]
    assert (
        json.loads((loop.directory / "reviews/before.json").read_text()) == corrected.model_dump()
    )


def test_invalid_prior_and_repeated_bad_drafts_are_never_accepted(task, tmp_path):
    prior = decision("Earlier ungrounded assessment", quote="invented outcome")
    original = prior.model_dump()
    model = ScriptedModel([prior, prior])
    loop = make_loop(tmp_path, model, max_read_rounds=1)
    with pytest.raises(ValueError, match="within the call limit"):
        loop._review(task, [], "before", probe_limit=0, prior=prior)
    assert len(model.calls) == 2
    assert all(call["payload"]["previous_review"] == original for call in model.calls)
    assert prior.model_dump() == original
    assert not (loop.directory / "reviews/before.json").exists()
    assert not (loop.directory / "repairs").exists()


def test_escalation_receives_latest_local_read_draft_not_outer_prior(task, tmp_path):
    prior = decision("Review from the previous phase")
    rejected = decision("First local draft", quote="invented outcome")
    requesting = decision("Corrected local draft requesting verifier source", read=True)
    completed = decision("Grounded decision after examining requested source")
    escalation = LLMSpec(provider="anthropic", model="claude-opus-4-6")
    model = ScriptedModel([rejected, requesting, completed])
    loop = make_loop(tmp_path, model, max_read_rounds=1, escalation_model=escalation)
    accepted, _ = loop._review(task, [], "after", probe_limit=0, prior=prior)
    assert model.calls[0]["payload"]["previous_review"] == prior.model_dump()
    assert model.calls[1]["payload"]["previous_review"] == rejected.model_dump()
    final = model.calls[2]
    assert final["model"] == escalation
    assert final["payload"]["previous_review"] == requesting.model_dump()
    assert "Requested file ranges are now included" in final["payload"]["protocol_feedback"][-1]
    assert any("independent sum check" in text for text in final["payload"]["documents"].values())
    assert final["payload"]["review_calls_remaining"] == 0
    assert accepted == completed
    assert [call["key"] for call in model.calls] == ["after-0", "after-1", "after-2"]


def test_unparsed_response_keeps_outer_prior_until_a_local_review_exists(task, tmp_path):
    prior = decision("Existing phase review")
    corrected = decision("First parsed local response")
    model = ScriptedModel([ValueError("Invalid JSON response"), corrected])
    loop = make_loop(tmp_path, model, max_read_rounds=1)
    accepted, _ = loop._review(task, [], "before", probe_limit=0, prior=prior)
    assert model.calls[1]["payload"]["previous_review"] == prior.model_dump()
    assert "Invalid JSON response" in model.calls[1]["payload"]["protocol_feedback"][-1]
    assert accepted == corrected
