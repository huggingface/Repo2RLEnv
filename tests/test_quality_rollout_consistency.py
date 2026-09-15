"""Reviewer outcome labels cannot contradict the supplied solver execution."""

from __future__ import annotations

import json

import pytest

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.quality.loop.artifacts import digest, task_identity
from repo2rlenv.quality.loop.models import Assessment, Citation, LoopOptions, Review, TrialRecord
from repo2rlenv.quality.loop.runner import QualityLoop
from repo2rlenv.spec.input import LLMSpec


@pytest.fixture
def task(tmp_path):
    return write_bundle(
        TaskBundle(
            name="rollout-consistency",
            org="tests",
            instruction="Implement the requested behavior.\n",
            metadata={"recipe": "test", "recipe_version": "1", "reward_kinds": ["test_execution"]},
            files={
                "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
                "solution/solve.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
                "tests/test.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
            },
        ),
        tmp_path / "tasks",
    )


def trial(task, tmp_path, reward, *, exception=None, key="solver", agent_exit_code=None):
    path = tmp_path / key / "result.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "config": {"agent": {"name": "terminus-2"}},
                "verifier_result": {"rewards": {"reward": reward}},
                "exception_info": {"exception_type": exception} if exception else None,
            }
        )
    )
    return TrialRecord(
        role="rollout",
        bundle_hash=task_identity(task),
        result=str(path),
        result_sha256=digest(path),
        agent="terminus-2",
        model=None,
        reward=reward,
        exception_type=exception,
        binding="receipt",
        agent_exit_code=agent_exit_code,
    )


class Model:
    def __init__(self, outcomes):
        self.outcomes, self.calls = iter(outcomes), []

    def ask(self, schema, model, system, user, key):
        assert schema is Review
        self.calls.append({"key": key, "model": model.qualified_name, **json.loads(user)})
        assessment = Assessment(
            status="pass",
            score=3,
            explanation="A meaningful behavioral task.",
            evidence=[Citation(path="instruction.md", quote="Implement the requested behavior.")],
        )
        return Review(
            summary="Assess the actual solver outcome.",
            task=assessment,
            verifier=assessment,
            leakage=assessment,
            rollout=next(self.outcomes),
            issues=[],
            probes=[],
            read_requests=[],
        )


def loop(tmp_path, model, **options):
    return QualityLoop(
        LoopOptions(max_read_rounds=1, **options),
        tmp_path / "quality",
        BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="10"),
        model_client=model,
    )


@pytest.mark.parametrize(
    "reward,claimed,corrected,success",
    [
        (0.0, "legitimate_success", "legitimate_failure", 1.0),
        (1.0, "legitimate_failure", "legitimate_success", 1.0),
        (0.5, "legitimate_failure", "legitimate_success", 0.5),
    ],
)
def test_reward_contradiction_uses_existing_review_correction(
    task, tmp_path, reward, claimed, corrected, success
):
    model = Model([claimed, corrected])
    result, _ = loop(tmp_path, model, success_reward=success)._review(
        task,
        [trial(task, tmp_path, reward)],
        "r0-after",
        probe_limit=0,
    )
    assert result.rollout == corrected
    assert [call["key"] for call in model.calls] == ["r0-after-0", "r0-after-1"]
    correction = model.calls[-1]
    assert correction["previous_review"]["rollout"] == claimed
    assert correction["review_calls_remaining"] == 0
    assert f"reward={reward}, success_reward={success}" in correction["protocol_feedback"][0]
    assert "evidence/0-rollout/result.json" in correction["protocol_feedback"][0]
    saved = json.loads((tmp_path / "quality/reviews/r0-after.json").read_text())
    assert saved["rollout"] == corrected


def test_repeated_reward_contradiction_exhausts_existing_limit(task, tmp_path):
    model = Model(["legitimate_success", "legitimate_success"])
    with pytest.raises(ValueError, match=r"within the call limit.*reward=0\.0"):
        loop(tmp_path, model)._review(task, [trial(task, tmp_path, 0.0)], "r0-after", probe_limit=0)
    assert len(model.calls) == 2
    assert not (tmp_path / "quality/reviews/r0-after.json").exists()


def test_reward_contradiction_can_use_only_configured_escalation(task, tmp_path):
    model = Model(["legitimate_success", "legitimate_success", "legitimate_failure"])
    reviewer = loop(
        tmp_path, model, escalation_model=LLMSpec(provider="anthropic", model="claude-opus-4-6")
    )
    result, _ = reviewer._review(task, [trial(task, tmp_path, 0.0)], "r0-after", probe_limit=0)
    assert result.rollout == "legitimate_failure"
    assert len(model.calls) == 3
    assert model.calls[-1]["model"] == "anthropic/claude-opus-4-6"


@pytest.mark.parametrize("claimed", ["legitimate_success", "legitimate_failure"])
def test_no_rollout_cannot_support_a_legitimate_label(task, tmp_path, claimed):
    model = Model([claimed, "not_run"])
    result, _ = loop(tmp_path, model)._review(task, [], "r0-before", probe_limit=0)
    assert result.rollout == "not_run"
    assert "no supplied rollout evidence" in model.calls[-1]["protocol_feedback"][0]


@pytest.mark.parametrize(
    "reward,exception,claimed,diagnosis",
    [
        (None, None, "legitimate_failure", "insufficient_evidence"),
        (1.0, "ProviderError", "legitimate_success", "infrastructure_failure"),
        (0.0, "VerifierTimeoutError", "legitimate_failure", "infrastructure_failure"),
    ],
)
def test_missing_reward_or_infrastructure_cannot_infer_legitimacy(
    task, tmp_path, reward, exception, claimed, diagnosis
):
    model = Model([claimed, diagnosis])
    result, _ = loop(tmp_path, model)._review(
        task,
        [trial(task, tmp_path, reward, exception=exception)],
        "r0-after",
        probe_limit=0,
    )
    assert result.rollout == diagnosis
    assert "is unsupported" in model.calls[-1]["protocol_feedback"][0]


@pytest.mark.parametrize("exception", [None, "AgentTimeoutError"])
def test_existing_bounded_solver_failure_semantics_are_preserved(task, tmp_path, exception):
    model = Model(["legitimate_failure"])
    result, _ = loop(tmp_path, model)._review(
        task,
        [trial(task, tmp_path, 0.0, exception=exception, agent_exit_code=1)],
        "r0-after",
        probe_limit=0,
    )
    assert result.rollout == "legitimate_failure"
    assert len(model.calls) == 1


@pytest.mark.parametrize(
    "outcome", ["reward_hack", "task_defect", "incomplete", "insufficient_evidence"]
)
def test_diagnostic_categories_remain_model_judgments(task, tmp_path, outcome):
    model = Model([outcome])
    result, _ = loop(tmp_path, model)._review(
        task,
        [trial(task, tmp_path, 1.0)],
        "r0-after",
        probe_limit=0,
    )
    assert result.rollout == outcome and len(model.calls) == 1


def test_latest_supplied_rollout_controls_consistency(task, tmp_path):
    model = Model(["legitimate_failure", "legitimate_success"])
    trials = [trial(task, tmp_path, 0.0, key="earlier"), trial(task, tmp_path, 1.0, key="latest")]
    result, _ = loop(tmp_path, model)._review(task, trials, "r0-after", probe_limit=0)
    assert result.rollout == "legitimate_success"
    assert "evidence/1-rollout/result.json" in model.calls[-1]["protocol_feedback"][0]
