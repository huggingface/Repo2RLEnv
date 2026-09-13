"""Repair feedback stays actionable, bounded, and faithful to retained probes."""

from __future__ import annotations

import json

import pytest

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.quality.loop.artifacts import task_identity
from repo2rlenv.quality.loop.client import ModelRequestError
from repo2rlenv.quality.loop.models import (
    Assessment,
    Citation,
    Edit,
    Issue,
    LoopOptions,
    Repair,
    Review,
    SemanticProbe,
)
from repo2rlenv.quality.loop.runner import QualityLoop


@pytest.fixture
def task(tmp_path):
    return write_bundle(
        TaskBundle(
            name="repair-protocol",
            org="tests",
            instruction="Compute the sum of two integers.\n",
            metadata={"recipe": "test", "recipe_version": "1", "reward_kinds": ["test_execution"]},
            files={
                "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
                "solution/solve.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
                "tests/test.sh": TaskFile.text("#!/bin/sh\n# weak check\n", executable=True),
            },
        ),
        tmp_path / "tasks",
    )


def probes():
    return [
        SemanticProbe(
            name=name,
            kind=kind,
            rationale="Exercise a concrete arithmetic behavior.",
            evidence=[Citation(path="instruction.md", quote="sum of two integers")],
            script=script,
        )
        for name, kind, script in [
            ("wrong-sum", "wrong_solution", "printf 7 > /workspace/answer.txt"),
            ("valid-format", "valid_alternative", "printf '4\\n' > /workspace/answer.txt"),
        ]
    ]


def review(category="verifier"):
    assessment = Assessment(
        status="pass",
        score=3,
        explanation="The task requests useful arithmetic behavior.",
        evidence=[Citation(path="instruction.md", quote="sum of two integers")],
    )
    return Review(
        summary="Repair a grounded verification issue.",
        task=assessment,
        verifier=assessment,
        leakage=assessment,
        rollout="not_run",
        probes=[],
        read_requests=[],
        issues=[
            Issue(
                category=category,
                severity="blocking",
                problem="The existing check rejects a valid representation.",
                repair="Check the requested observable behavior.",
                evidence=[Citation(path="tests/test.sh", quote="weak check")],
            )
        ],
    )


def patch(*, old="weak check", replacements=()):
    return Repair(
        explanation="Correct the check without weakening the arithmetic contract.",
        addressed_issues=["The existing check rejects a valid representation."],
        edits=[Edit(path="tests/test.sh", old=old, new="strong check", executable=True)],
        probe_replacements=list(replacements),
    )


class Model:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []

    def ask(self, schema, model, system, user, key):
        assert schema is Repair
        self.calls.append({"key": key, **json.loads(user)})
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return schema.model_validate(
            response.model_dump() if isinstance(response, Repair) else response
        )


def loop(tmp_path, model):
    return QualityLoop(
        LoopOptions(repair=True),
        tmp_path / "quality",
        BudgetLedger(tmp_path / "budget.sqlite3", limit_usd="10"),
        model_client=model,
    )


def test_verifier_repair_correction_retains_probes_and_receives_rejected_draft(task, tmp_path):
    retained = probes()
    rejected = patch(replacements=[retained[1]])
    corrected = patch()
    model = Model([rejected, corrected])
    runner = loop(tmp_path, model)
    original_hash = task_identity(task)
    destination, updated = runner._repair(
        task, review(), runner._context(task, []), retained, 0, []
    )
    correction = model.calls[1]
    assert correction["previous_repair"] == rejected.model_dump()
    assert correction["probe_replacement_policy"]["allowed_replacements"] == []
    assert correction["probe_replacement_policy"]["immutable_wrong_solution_probes"] == [
        "wrong-sum"
    ]
    assert "Set probe_replacements=[]" in correction["patch_feedback"][0]
    assert correction["correction_calls_remaining"] == 0
    assert updated == retained
    assert task_identity(task) == original_hash != task_identity(destination)
    assert (task / "tests/test.sh").read_text().endswith("# weak check\n")
    assert (destination / "tests/test.sh").read_text().endswith("# strong check\n")
    assert [call["key"] for call in model.calls] == ["r0-repair", "r0-repair-correction1"]


def test_wrong_probe_replacement_stays_forbidden_even_with_probe_diagnosis(task, tmp_path):
    retained = probes()
    invalid = patch().model_dump()
    invalid["probe_replacements"] = [retained[0].model_dump()]
    model = Model([invalid, invalid])
    runner = loop(tmp_path, model)
    original_hash = task_identity(task)
    with pytest.raises(ValueError, match="wrong-solution probes cannot be replaced"):
        runner._repair(task, review("probe"), runner._context(task, []), retained, 0, [])
    assert len(model.calls) == 2
    assert model.calls[1]["previous_repair"] is None
    policy = model.calls[1]["probe_replacement_policy"]
    assert policy["allowed_replacements"] == [
        {"name": "valid-format", "kind": "valid_alternative", "focus": "general"}
    ]
    assert "no-op probe" in policy["rule"]
    assert task_identity(task) == original_hash
    assert not (runner.directory / "revisions/r1").exists()


@pytest.mark.parametrize("corrected", [True, False])
def test_excerpt_budget_failure_uses_existing_correction_without_inventing_source(
    task, tmp_path, monkeypatch, corrected
):
    rejected = patch(old="missing source text")
    model = Model([rejected, patch() if corrected else rejected])
    runner = loop(tmp_path, model)
    context = runner._context(task, [])
    documents = dict(context.documents)

    def unavailable(requests):
        raise ValueError("Requested excerpts exceed context limit")

    monkeypatch.setattr(context, "read_more", unavailable)
    if corrected:
        destination, _ = runner._repair(task, review(), context, probes(), 0, [])
        assert (destination / "tests/test.sh").read_text().endswith("# strong check\n")
    else:
        with pytest.raises(ValueError, match="old text must occur exactly once"):
            runner._repair(task, review(), context, probes(), 0, [])
        assert not (runner.directory / "revisions/r1" / task.name).exists()
    assert len(model.calls) == 2
    correction = model.calls[1]
    assert correction["documents"] == documents
    assert correction["previous_repair"] == rejected.model_dump()
    assert any(
        "Repair-source excerpt unavailable for tests/test.sh" in feedback
        and "Requested excerpts exceed context limit" in feedback
        for feedback in correction["patch_feedback"]
    )


def test_provider_uncertainty_never_triggers_protocol_redispatch(task, tmp_path):
    model = Model([ModelRequestError("Provider response uncertain")])
    runner = loop(tmp_path, model)
    with pytest.raises(ModelRequestError, match="uncertain"):
        runner._repair(task, review(), runner._context(task, []), probes(), 0, [])
    assert len(model.calls) == 1
    assert not (runner.directory / "revisions/r1").exists()
