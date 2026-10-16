"""Review semantics and evidence integrity without provider or target execution."""

import json

import pytest
from pydantic import ValidationError

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.quality.loop.context import EvidenceContext
from repo2rlenv.quality.loop.models import Citation, LoopOptions, LoopResult
from repo2rlenv.quality.loop.runner import QualityLoop
from repo2rlenv.quality.reviewer.models import CRITERIA, Criterion, EvidenceReview, Finding
from repo2rlenv.quality.reviewer.profile import load_review


@pytest.fixture
def task(tmp_path):
    return write_bundle(
        TaskBundle(
            name="review-fixture",
            org="tests",
            instruction="Return the sum of two integers.\n",
            metadata={"recipe": "test", "recipe_version": "1", "reward_kinds": ["test_execution"]},
            files={
                "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
                "tests/test.sh": TaskFile.text("#!/bin/sh\n# Verify the sum\n", executable=True),
                "solution/solve.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
            },
        ),
        tmp_path / "tasks",
    )


def decision(*, fail=False, basis="static", attribution="task"):
    evidence = [Citation(path="instruction.md", quote="sum of two integers")]
    return EvidenceReview(
        summary="Review the arithmetic contract",
        criteria=[
            Criterion(
                criterion=key,
                status="fail" if fail and key == "coverage" else "pass",
                score=1 if fail and key == "coverage" else 3,
                explanation="Contract inspected",
                evidence=evidence,
            )
            for key in CRITERIA
        ],
        rollout="not_run",
        probes=[],
        read_requests=[],
        investigations=[],
        issues=[
            Finding(
                criterion="coverage",
                category="verifier",
                severity="blocking",
                attribution=attribution,
                confidence="high",
                basis=basis,
                consequence="An incorrect sum can pass",
                problem="Missing assertion",
                repair="Check arithmetic",
                evidence=evidence,
            )
        ]
        if fail
        else [],
    )


def test_failed_criterion_requires_issue():
    data = decision(fail=True).model_dump()
    data["issues"] = []
    with pytest.raises(ValidationError, match="blocking finding"):
        EvidenceReview.model_validate(data)


def test_pass_cannot_contain_blocking_finding():
    data = decision(fail=True).model_dump()
    for item in data["criteria"]:
        item["status"] = "pass"
    with pytest.raises(ValidationError, match="require criterion"):
        EvidenceReview.model_validate(data)


@pytest.mark.parametrize("attribution", ["solver", "unresolved", "infrastructure"])
def test_solver_or_provider_failure_is_not_task_defect(attribution):
    with pytest.raises(ValidationError, match="not a task defect"):
        decision(fail=True, attribution=attribution)


def test_every_criterion_must_be_present():
    data = decision().model_dump()
    data["criteria"][-1] = data["criteria"][0]
    with pytest.raises(ValidationError, match="exactly once"):
        EvidenceReview.model_validate(data)


def test_aggregate_cannot_hide_unknown():
    data = decision().model_dump()
    data["criteria"][3]["status"] = "unknown"
    result = EvidenceReview.model_validate(data).legacy()
    assert result.verifier.status == "unknown"
    assert not result.sound


def test_observation_requires_bound_trial(task):
    context = EvidenceContext(task, [], limit=16000)
    with pytest.raises(ValueError, match="completed bound trial"):
        decision(fail=True, basis="observed").validate_evidence(context, [])


def test_citation_and_hypothesis_are_both_grounded(task):
    context = EvidenceContext(task, [], limit=16000)
    raw = decision().model_dump()
    raw["investigations"] = [
        {
            "hypothesis": "Could arithmetic be wrong?",
            "outcome": "dismissed",
            "explanation": "Checked",
            "evidence": [{"path": "instruction.md", "quote": "fabricated excerpt"}],
        }
    ]
    with pytest.raises(ValueError, match="Ungrounded investigation"):
        EvidenceReview.model_validate(raw).validate_evidence(context, [])


class Model:
    def __init__(self):
        self.calls = 0

    def ask(self, schema, model, system, user, key):
        assert schema is EvidenceReview
        assert "evidence-review-v2" in system
        assert "expected_defect" not in user
        self.calls += 1
        return decision()


def test_opt_in_loop_preserves_legacy_view_and_integrity(task, tmp_path):
    model = Model()
    root = tmp_path / "review"
    loop = QualityLoop(
        LoopOptions(profile="evidence-review-v2", max_probes=0),
        root,
        BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="10"),
        model_client=model,
    )
    result = loop.run(task)
    assert result.schema_version == "2" and result.status == "reviewed"
    assert result.review.sound and model.calls == 1
    assert LoopResult.model_validate_json((root / "result.json").read_text()) == result
    record, evidence, reviewer = load_review(root)
    assert record["task_hash"] == result.bundle_hash
    assert reviewer["profile"] == "evidence-review-v2"
    assert "sum of two integers" in evidence["documents"]["instruction.md"]
    details = root / json.loads((root / "findings.json").read_text())["latest"]
    path = details / "evidence.json"
    data = json.loads(path.read_text())
    data["documents"]["instruction.md"] = "tampered"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="identity changed"):
        load_review(root)


def test_result_profile_requires_correct_schema():
    raw = dict(
        status="needs_evidence",
        source_hash="a",
        bundle_hash="a",
        task_path="/tmp/a",
        repairs=0,
        review=None,
        trials=[],
        reasons=[],
        accounted_usd="0",
        reserved_usd="0",
    )
    assert LoopResult(**raw).profile == "practical-generation-v1"
    with pytest.raises(ValidationError, match="does not match"):
        LoopResult(**raw, profile="evidence-review-v2")


def test_unresolved_review_does_not_trigger_task_repair(task, tmp_path):
    class UnknownModel:
        def ask(self, schema, model, system, user, key):
            raw = decision().model_dump()
            raw["criteria"][3]["status"] = "unknown"
            return EvidenceReview.model_validate(raw)

    result = QualityLoop(
        LoopOptions(profile="evidence-review-v2", max_probes=0),
        tmp_path / "unresolved",
        BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="1"),
        model_client=UnknownModel(),
    ).run(task)
    assert result.status == "needs_evidence"
    assert result.repairs == 0
