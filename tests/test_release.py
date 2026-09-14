from __future__ import annotations

import json
import tarfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from repo2rlenv.campaigns.release import ReleasePlan, publish_release, stage_release, verify_release
from repo2rlenv.emitter.bundle import TaskBundle, TaskFile, inspect_bundle, write_bundle


@pytest.fixture
def selection(tmp_path):
    task = write_bundle(
        TaskBundle(
            name="example",
            org="test",
            instruction="Write the requested report.",
            metadata={
                "recipe": "example",
                "recipe_version": "1",
                "reward_kinds": ["test_execution"],
                "quality_status": "exported",
            },
            files={
                "environment/Dockerfile": TaskFile.text("FROM python:3.12-slim\n"),
                "solution/solve.sh": TaskFile.text("#!/bin/sh\nexit 0\n", executable=True),
                "tests/test.sh": TaskFile.text("#!/bin/sh\nexit 1\n", executable=True),
            },
        ),
        tmp_path / "generated",
    )
    return ReleasePlan(
        repo_id="org/example",
        recipe="example",
        title="Example",
        description="A test release.",
        methodology="Owned test fixture.",
        code_revision="test",
        tasks=[{"path": task, "bundle_hash": inspect_bundle(task)["bundle_hash"]}],
    )


def test_release_archive_roundtrip_preserves_modes_identity_and_labels(selection, tmp_path):
    stage = tmp_path / "stage"
    stage_release(selection, stage)
    verify_release(stage)
    with tarfile.open(stage / "tasks.tar.gz") as archive:
        archive.extractall(tmp_path / "download", filter="data")
    task = tmp_path / "download/tasks/example"
    assert inspect_bundle(task)["bundle_hash"] == selection.tasks[0].bundle_hash
    assert inspect_bundle(task)["integrity_passed"]
    assert (task / "solution/solve.sh").stat().st_mode & 0o777 == 0o755
    assert json.loads((stage / "manifest.json").read_text())["quality_counts"] == {"exported": 1}
    assert inspect_bundle(selection.tasks[0].path)["integrity_passed"]


def test_no_publication_for_changed_staging(selection, tmp_path):
    stage = tmp_path / "stage"
    stage_release(selection, stage)
    (stage / "tasks/example/tests/test.sh").write_text("changed")
    api = Mock()
    with pytest.raises(ValueError, match="Staged release changed"):
        publish_release(stage, api=api, receipt=tmp_path / "receipt.json")
    assert not api.mock_calls


def test_changed_source_or_duplicate_selection_cannot_be_staged(selection, tmp_path):
    duplicate = selection.model_copy(update={"tasks": selection.tasks * 2})
    with pytest.raises(ValueError, match="duplicate"):
        stage_release(duplicate, tmp_path / "duplicate")
    (selection.tasks[0].path / "instruction.md").write_text("changed")
    with pytest.raises(ValueError, match="Task content changed"):
        stage_release(selection, tmp_path / "changed")
    assert not (tmp_path / "changed").exists()


def test_uncertain_upload_is_not_replayed(selection, tmp_path):
    stage = tmp_path / "stage"
    stage_release(selection, stage)
    api = Mock()
    api.upload_folder.side_effect = TimeoutError("uncertain commit")
    receipt = tmp_path / "receipt.json"
    with pytest.raises(TimeoutError):
        publish_release(stage, api=api, receipt=receipt)
    with pytest.raises(ValueError, match="needs reconciliation"):
        publish_release(stage, api=api, receipt=receipt)
    assert api.upload_folder.call_count == 1


def test_publication_pins_registry_and_records_collection(selection, tmp_path):
    stage = tmp_path / "stage"
    stage_release(selection, stage)
    api = Mock()
    api.upload_folder.return_value = SimpleNamespace(oid="abc123")
    api.list_repo_files.return_value = [
        "tasks/example/task.toml",
        "manifest.json",
        "tasks.tar.gz",
        "data/tasks.jsonl",
    ]
    receipt = tmp_path / "receipt.json"
    first = publish_release(stage, api=api, receipt=receipt, collection_slug="org/collection")
    assert first["state"] == "completed"
    registry = json.loads(api.upload_file.call_args.kwargs["path_or_fileobj"])
    assert registry[0]["tasks"][0]["git_commit_id"] == "abc123"
    api.add_collection_item.assert_called_once()
    assert publish_release(stage, api=api, receipt=receipt) == first
    assert api.upload_folder.call_count == 1
