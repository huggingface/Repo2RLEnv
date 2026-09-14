"""Probe requirements create a new review input without rebinding old evidence."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.quality.loop.artifacts import task_identity
from repo2rlenv.quality.loop.requirements import with_probe_requirements
from repo2rlenv.tasksmith import runner as runner_module
from repo2rlenv.tasksmith.models import Options


@pytest.fixture
def review_setup(tmp_path, monkeypatch):
    campaign = tmp_path / "campaign"
    BudgetLedger(campaign / "budget.sqlite3", limit_usd="100")
    runner = runner_module.Tasksmith(
        tmp_path / "run", campaign, Options(gpus=1), tmp_path / "runtime.whl"
    )
    task = write_bundle(
        TaskBundle(
            name="fixture",
            org="tests",
            instruction="Restore the specified public behavior.",
            metadata={
                "recipe": "tasksmith",
                "recipe_version": "1",
                "reward_kinds": ["test_execution"],
            },
            files={
                "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
                "tests/test.sh": TaskFile.text("#!/bin/sh\nexit 1\n", executable=True),
                "solution/solve.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
            },
        ),
        tmp_path / "constructed",
    )
    calls = []

    class Loop:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, task, **kwargs):
            calls.append({"task": task, **kwargs})
            return SimpleNamespace(
                status="reviewed", model_dump=lambda **kwargs: {"status": "reviewed"}
            )

    from repo2rlenv.quality.loop import native

    monkeypatch.setattr(runner_module, "QualityLoop", Loop)
    monkeypatch.setattr(native, "NativeModalTrials", lambda *args, **kwargs: None)
    return runner, task, calls


def review(runner, task):
    root = runner.directory / "candidate"
    constructed = {
        "local": str(task.parent),
        "value": {"task_relative": task.name},
        "imported_from": {
            "probes": [{"kind": "wrong_solution", "description": "Previously tested probe"}],
            "trials": {
                role: {
                    "result": str(root / "original-evidence" / f"{role}.json"),
                    "model": runner.options.quality.solver_model.qualified_name,
                }
                for role in ("baseline", "oracle", "rollout")
            },
        },
    }
    runner.review_candidate(root, {"id": "abcdefgh1234"}, constructed)
    return root


def test_default_requirements_keep_input_and_imported_evidence(review_setup):
    runner, task, calls = review_setup
    root = review(runner, task)
    assert calls[0]["task"] == task
    assert calls[0]["probes"] == root / "imported-probes.json"
    assert all(isinstance(calls[0][role], Path) for role in ("baseline", "oracle", "rollout"))
    assert not (root / "quality-input.json").exists()


def test_new_requirements_copy_input_and_discard_prior_proofs(review_setup):
    runner, task, calls = review_setup
    runner.options.required_probe_focus = ["compiled_execution"]
    before = {
        str(path.relative_to(task)): path.read_bytes() for path in task.rglob("*") if path.is_file()
    }
    old_hash = task_identity(task)
    root = review(runner, task)
    annotated = root / "quality-input" / task.name
    assert calls[0]["task"] == annotated and calls[0]["probes"] is None
    assert all(role not in calls[0] for role in ("baseline", "oracle", "rollout"))
    assert not (root / "imported-probes.json").exists()
    assert task_identity(task) == old_hash != task_identity(annotated)
    assert before == {
        str(path.relative_to(task)): path.read_bytes() for path in task.rglob("*") if path.is_file()
    }
    receipt = json.loads((root / "quality-input.json").read_text())
    assert receipt == {
        "source_task": str(task.resolve()),
        "source_bundle_hash": old_hash,
        "task_path": str(annotated.resolve()),
        "bundle_hash": task_identity(annotated),
        "required_probe_focus": ["compiled_execution"],
        "imported_evidence_reused": False,
    }
    review(runner, task)
    assert calls[1] == calls[0]


def test_existing_requirements_keep_exact_identity_and_imported_evidence(review_setup):
    runner, task, calls = review_setup
    runner.options.required_probe_focus = ["compiled_execution"]
    annotated = with_probe_requirements(
        task, task.parent / "previous-input", ["compiled_execution"]
    )
    root = review(runner, annotated)
    assert calls[0]["task"] == annotated
    assert calls[0]["probes"] == root / "imported-probes.json"
    assert all(role in calls[0] for role in ("baseline", "oracle", "rollout"))
    assert not (root / "quality-input.json").exists()


def test_invalid_required_input_prevents_quality_dispatch(review_setup):
    runner, task, calls = review_setup
    runner.options.required_probe_focus = ["compiled_execution"]
    (task / "instruction.md").write_text("Changed after generation.")
    with pytest.raises(ValueError, match="recorded bundle hash"):
        review(runner, task)
    assert calls == []
