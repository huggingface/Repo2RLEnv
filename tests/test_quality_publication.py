"""Publication preserves evidence and detects stale or edited labeled exports."""

from __future__ import annotations

import json
import tomllib

import pytest
import tomli_w

from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.quality.loop.artifacts import task_identity
from repo2rlenv.quality.loop.models import LoopResult
from repo2rlenv.quality.loop.publication import publish_label


@pytest.fixture
def quality(tmp_path):
    task = write_bundle(
        TaskBundle(
            name="publication",
            org="tests",
            instruction="Restore the expected behavior.",
            metadata={
                "recipe": "fixture",
                "recipe_version": "1",
                "reward_kinds": ["test_execution"],
            },
            files={
                "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
                "solution/solve.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
                "tests/test.sh": TaskFile.text("#!/bin/sh\nexit 1\n", executable=True),
            },
        ),
        tmp_path / "source",
    )
    output = tmp_path / "quality"
    output.mkdir()
    result = LoopResult(
        status="needs_repair",
        source_hash=task_identity(task),
        bundle_hash=task_identity(task),
        task_path=str(task),
        repairs=0,
        review=None,
        trials=[],
        reasons=["Oracle failed the private test"],
        accounted_usd="0",
        reserved_usd="0",
    )
    (output / "result.json").write_text(result.model_dump_json())
    return task, output


def test_publication_is_idempotent_and_preserves_raw_result(quality):
    from pathlib import Path

    task, output = quality
    before = (output / "result.json").read_bytes(), (task / "task.toml").read_bytes()
    first = publish_label(output)
    assert first["state"] == "completed" and first["status"] == "needs_repair"
    assert publish_label(output) == first
    assert before == ((output / "result.json").read_bytes(), (task / "task.toml").read_bytes())
    assert task_identity(Path(first["task_path"])) == task_identity(task)


def test_existing_label_tampering_fails_without_overwriting_quality(quality):
    from pathlib import Path

    _, output = quality
    first = publish_label(output)
    target = Path(first["task_path"]) / "task.toml"
    config = tomllib.loads(target.read_text())
    config["metadata"]["repo2env"]["evaluation"]["detail"] = "Different conclusion"
    target.write_text(tomli_w.dumps(config))
    before = (output / "result.json").read_bytes()
    assert publish_label(output)["state"] == "failed"
    assert (output / "result.json").read_bytes() == before


def test_missing_task_records_export_failure_and_keeps_completed_result(quality):
    task, output = quality
    (task / "instruction.md").unlink()
    before = (output / "result.json").read_bytes()
    assert publish_label(output)["state"] == "failed"
    assert json.loads((output / "labeled-task.json").read_text())["state"] == "failed"
    assert (output / "result.json").read_bytes() == before


def test_content_revision_discards_inherited_evaluation(quality):
    from pathlib import Path

    from repo2rlenv.quality.labels import read_evaluation
    from repo2rlenv.quality.loop.artifacts import refresh_identity

    _, output = quality
    task = Path(publish_label(output)["task_path"])
    assert read_evaluation(task).status == "needs_repair"
    (task / "instruction.md").write_text("Clarified behavior for a new revision.\n")
    identity = refresh_identity(task)
    label = read_evaluation(task)
    assert label.status == "unverified" and label.reason_codes == ["task_changed"]
    assert label.subject_bundle_hash == identity and not label.evidence
