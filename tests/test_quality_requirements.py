from __future__ import annotations

import hashlib
import stat
import tomllib

import pytest
import tomli_w

from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, write_bundle
from repo2rlenv.quality.loop.artifacts import refresh_identity, task_identity
from repo2rlenv.quality.loop.requirements import task_probe_focus, with_probe_requirements
from repo2rlenv.tasksmith.models import Options


@pytest.fixture
def task(tmp_path):
    return write_bundle(
        TaskBundle(
            name="compiled-example",
            org="tests",
            instruction="Return a compiled model whose outputs preserve the original model.\n",
            files={
                "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
                "solution/solve.sh": TaskFile.text("#!/bin/sh\ntrue\n", executable=True),
                "tests/test.sh": TaskFile.text("#!/bin/sh\ntrue\n", executable=True),
            },
            metadata={"recipe": "test", "recipe_version": "1", "reward_kinds": ["test_execution"]},
        ),
        tmp_path / "tasks",
    )


def contents(task):
    return {
        str(path.relative_to(task)): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            stat.S_IMODE(path.stat().st_mode),
        )
        for path in task.rglob("*")
        if path.is_file()
    }


def set_requirement(task, value):
    path = task / "task.toml"
    config = tomllib.loads(path.read_text())
    config["metadata"]["repo2env"]["quality_requirements"] = value
    path.write_text(tomli_w.dumps(config))


def test_legacy_task_has_no_implicit_compilation_requirement(task, tmp_path):
    before = contents(task)
    destination = tmp_path / "unused"
    assert task_probe_focus(task) == set()
    assert with_probe_requirements(task, destination, []) == task
    assert contents(task) == before
    assert not destination.exists()


@pytest.mark.parametrize(
    "value",
    [
        {"probe_focus": ["unknown"]},
        {"probe_focus": ["compiled_execution", "compiled_execution"]},
        {"probe_focus": "compiled_execution"},
        {"probe_focus": [True]},
        {"probe_focus": ["compiled_execution"], "ignore_errors": True},
        "compiled_execution",
    ],
)
def test_task_requirement_metadata_is_strict(task, value):
    set_requirement(task, value)
    with pytest.raises(ValueError):
        task_probe_focus(task)


def test_requirement_is_hashed_while_evaluation_is_advisory(task):
    original = task_identity(task)
    set_requirement(task, {"probe_focus": ["compiled_execution"]})
    with pytest.raises(ValueError, match="recorded bundle hash"):
        task_identity(task)
    changed = refresh_identity(task)
    assert changed != original
    assert task_probe_focus(task) == {"compiled_execution"}
    path = task / "task.toml"
    config = tomllib.loads(path.read_text())
    config["metadata"]["repo2env"]["evaluation"]["detail"] = "An advisory diagnostic changed."
    path.write_text(tomli_w.dumps(config))
    assert task_identity(task) == changed


def test_annotated_copy_preserves_source_files_and_clears_prior_evaluation(task, tmp_path):
    config = tomllib.loads((task / "task.toml").read_text())
    config["metadata"]["repo2env"]["evaluation"]["status"] = "verified"
    (task / "task.toml").write_text(tomli_w.dumps(config))
    before, original = contents(task), task_identity(task)
    destination = tmp_path / "review-input" / task.name
    result = with_probe_requirements(task, destination, ["compiled_execution"])
    assert result == destination
    assert contents(task) == before
    assert task_identity(task) == original
    assert task_identity(result) != original
    assert task_probe_focus(result) == {"compiled_execution"}
    assert {k: v for k, v in contents(result).items() if k != "task.toml"} == {
        k: v for k, v in before.items() if k != "task.toml"
    }
    label = tomllib.loads((result / "task.toml").read_text())["metadata"]["repo2env"]["evaluation"]
    assert label["status"] == "unverified"
    assert label["reason_codes"] == ["task_changed"]
    assert label["subject_bundle_hash"] == task_identity(result)
    assert label["evidence"] == []


def test_annotated_input_resumes_and_requirement_subset_returns_original(task, tmp_path):
    destination = tmp_path / "review-input" / task.name
    result = with_probe_requirements(task, destination, ["compiled_execution", "numeric_tolerance"])
    before = contents(result)
    assert (
        with_probe_requirements(task, destination, ["numeric_tolerance", "compiled_execution"])
        == result
    )
    assert contents(result) == before
    unused = tmp_path / "unused"
    assert with_probe_requirements(result, unused, ["compiled_execution"]) == result
    assert not unused.exists()
    extended = with_probe_requirements(result, tmp_path / "extended", ["lazy_output"])
    assert task_probe_focus(extended) == {"compiled_execution", "numeric_tolerance", "lazy_output"}
    assert contents(result) == before


def test_conflicting_destination_is_not_overwritten(task, tmp_path):
    destination = with_probe_requirements(task, tmp_path / "destination", ["numeric_tolerance"])
    before = contents(destination)
    with pytest.raises(ValueError, match="different task content or requirements"):
        with_probe_requirements(task, destination, ["compiled_execution"])
    assert contents(destination) == before


def test_annotated_destination_cannot_modify_source(task):
    before = contents(task)
    for destination in [task, task / "nested"]:
        with pytest.raises(ValueError, match="outside the original task"):
            with_probe_requirements(task, destination, ["compiled_execution"])
    assert contents(task) == before


def test_unknown_requested_focus_fails_before_copy(task, tmp_path):
    destination = tmp_path / "destination"
    before = contents(task)
    with pytest.raises(ValueError):
        with_probe_requirements(task, destination, ["unknown"])
    assert contents(task) == before
    assert not destination.exists()


@pytest.mark.parametrize("resume", [False, True])
def test_source_change_during_annotation_prevents_publish_or_replay(
    task, tmp_path, monkeypatch, resume
):
    from repo2rlenv.quality.loop import requirements

    destination = tmp_path / "destination"
    if resume:
        with_probe_requirements(task, destination, ["compiled_execution"])
    before = contents(destination) if resume else None
    original_refresh = requirements.refresh_identity

    def source_changed(copied):
        identity = original_refresh(copied)
        (task / "instruction.md").write_text("The original changed during the copy.\n")
        return identity

    monkeypatch.setattr(requirements, "refresh_identity", source_changed)
    with pytest.raises(ValueError, match="recorded bundle hash"):
        with_probe_requirements(task, destination, ["compiled_execution"])
    if resume:
        assert contents(destination) == before
    else:
        assert not destination.exists()


def test_explicit_options_validate_focus_and_preserve_default():
    assert Options().required_probe_focus == []
    assert Options(
        required_probe_focus=["numeric_tolerance", "compiled_execution"]
    ).required_probe_focus == ["compiled_execution", "numeric_tolerance"]
    for invalid in [["unknown"], ["compiled_execution", "compiled_execution"]]:
        with pytest.raises(ValueError):
            Options(required_probe_focus=invalid)
