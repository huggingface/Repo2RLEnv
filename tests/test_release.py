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
    manifest = json.loads((stage / "manifest.json").read_text())
    assert manifest["quality_counts"] == {"unverified": 1}
    assert manifest["tasks"][0]["generation_status"] == "exported"
    assert inspect_bundle(selection.tasks[0].path)["integrity_passed"]


@pytest.mark.parametrize("normalize", [False, True])
def test_legacy_label_normalization_preserves_original_and_executable_identity(
    selection, tmp_path, normalize
):
    import tomllib

    import tomli_w

    source = selection.tasks[0].path
    config_path = source / "task.toml"
    config = tomllib.loads(config_path.read_text())
    del config["metadata"]["repo2env"]["evaluation"]
    config_path.write_text(tomli_w.dumps(config))
    original = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    stage = tmp_path / "stage"
    report = stage_release(
        selection.model_copy(update={"normalize_evaluation_labels": normalize}), stage
    )
    task = stage / "tasks/example"
    assert inspect_bundle(task) == inspect_bundle(source)
    assert report["quality_counts"] == {"unverified" if normalize else "exported": 1}
    assert report["tasks"][0]["evaluation_label_normalized"] is normalize
    metadata = tomllib.loads((task / "task.toml").read_text())["metadata"]["repo2env"]
    assert metadata["quality_status"] == "exported"
    if normalize:
        label = metadata["evaluation"]
        assert label["status"] == "unverified"
        assert label["subject_bundle_hash"] == selection.tasks[0].bundle_hash
        assert "checked_at" not in label
        assert label["evidence"] == []
    else:
        assert "evaluation" not in metadata
    for relative, content in original.items():
        assert (source / relative).read_bytes() == content
        if relative.as_posix() != "task.toml" or not normalize:
            assert (task / relative).read_bytes() == content
    verify_release(stage)


def test_no_publication_for_changed_staging(selection, tmp_path):
    stage = tmp_path / "stage"
    stage_release(selection, stage)
    (stage / "tasks/example/tests/test.sh").write_text("changed")
    api = Mock()
    with pytest.raises(ValueError, match="Staged release changed"):
        publish_release(stage, api=api, receipt=tmp_path / "receipt.json")
    assert not api.mock_calls


def test_normalization_preserves_existing_diagnosis(selection, tmp_path):
    import tomllib

    import tomli_w

    source = selection.tasks[0].path / "task.toml"
    config = tomllib.loads(source.read_text())
    config["metadata"]["repo2env"]["evaluation"].update(
        status="needs_repair", stage="review", reason_codes=["verifier_behavior_gap"]
    )
    source.write_text(tomli_w.dumps(config))
    original = source.read_bytes()
    stage = tmp_path / "stage"
    report = stage_release(
        selection.model_copy(update={"normalize_evaluation_labels": True}), stage
    )
    assert report["quality_counts"] == {"needs_repair": 1}
    assert not report["tasks"][0]["evaluation_label_normalized"]
    assert (stage / "tasks/example/task.toml").read_bytes() == original


def test_nested_delivery_task_keeps_package_identity_when_released(selection, tmp_path):
    selected = selection.tasks[0]
    nested = tmp_path / "delivery" / "task"
    nested.parent.mkdir()
    selected.path.rename(nested)
    selected.path = nested
    selected.task_id = "example"
    stage = tmp_path / "stage"
    stage_release(selection, stage)
    assert inspect_bundle(stage / "tasks/example")["bundle_hash"] == selected.bundle_hash
    assert not (stage / "tasks/task").exists()
    selected.task_id = "different"
    with pytest.raises(ValueError, match="must match the Harbor package name"):
        stage_release(selection, tmp_path / "mismatch")


def test_release_rejects_evaluation_from_another_task_revision(selection, tmp_path):
    import tomllib

    import tomli_w

    path = selection.tasks[0].path / "task.toml"
    config = tomllib.loads(path.read_text())
    config["metadata"]["repo2env"]["evaluation"]["subject_bundle_hash"] = "sha256:" + "0" * 64
    path.write_text(tomli_w.dumps(config))
    # Advisory metadata is outside the executable identity, so publication must
    # independently reject a label attached to the wrong revision.
    assert inspect_bundle(path.parent)["integrity_passed"]
    with pytest.raises(ValueError, match="Evaluation label belongs to a different"):
        stage_release(selection, tmp_path / "stage")


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
    api.list_repo_files.return_value = [*verify_release(stage)["files"], "release-files.json"]
    receipt = tmp_path / "receipt.json"
    first = publish_release(stage, api=api, receipt=receipt, collection_slug="org/collection")
    assert first["state"] == "completed"
    registry = json.loads(api.upload_file.call_args.kwargs["path_or_fileobj"])
    assert registry[0]["tasks"][0]["git_commit_id"] == "abc123"
    api.add_collection_item.assert_called_once()
    assert publish_release(stage, api=api, receipt=receipt) == first
    assert api.upload_folder.call_count == 1


def test_dataset_card_links_to_complete_harbor_bundles(selection, tmp_path):
    import yaml

    stage = tmp_path / "stage"
    stage_release(selection, stage)
    card = (stage / "README.md").read_text()
    metadata = yaml.safe_load(card.split("---")[1])
    assert (
        metadata["license_link"]
        == "https://huggingface.co/datasets/org/example/blob/main/LICENSES.md"
    )
    assert metadata["viewer"] is False
    assert "configs" not in metadata
    assert "harbor-visualiser?dataset=org/example" in card
    assert "https://huggingface.co/datasets/org/example/tree/main/tasks" in card
    for name in ("task.toml", "instruction.md", "tests/test.sh", "solution/solve.sh"):
        assert f"https://huggingface.co/datasets/org/example/blob/main/tasks/example/{name}" in card


def test_incomplete_task_cannot_be_advertised_in_registry_or_collection(selection, tmp_path):
    stage = tmp_path / "stage"
    stage_release(selection, stage)
    api = Mock()
    api.upload_folder.return_value = SimpleNamespace(oid="incomplete")
    api.list_repo_files.return_value = sorted(
        (set(verify_release(stage)["files"]) | {"release-files.json"})
        - {"tasks/example/tests/test.sh"}
    )
    with pytest.raises(RuntimeError, match="missing 1 release files"):
        publish_release(
            stage, api=api, receipt=tmp_path / "receipt.json", collection_slug="org/collection"
        )
    api.upload_file.assert_not_called()
    api.add_collection_item.assert_not_called()


def test_empty_upload_recovery_refuses_existing_remote_artifacts(selection, tmp_path):
    from repo2rlenv.campaigns.release import recover_empty_upload

    stage = tmp_path / "stage"
    stage_release(selection, stage)
    api = Mock()
    api.upload_folder.side_effect = TimeoutError()
    receipt = tmp_path / "receipt.json"
    with pytest.raises(TimeoutError):
        publish_release(stage, api=api, receipt=receipt)
    api.repo_info.return_value = SimpleNamespace(sha="observed")
    api.list_repo_files.return_value = [".gitattributes", "README.md"]
    with pytest.raises(ValueError, match="Remote artifacts exist"):
        recover_empty_upload(stage, api=api, receipt=receipt)
    api.create_commit.assert_not_called()


def test_empty_upload_recovery_pins_each_parent_and_finishes_registry(selection, tmp_path):
    from repo2rlenv.campaigns.release import recover_empty_upload

    stage = tmp_path / "stage"
    stage_release(selection, stage)
    api = Mock()
    api.upload_folder.side_effect = TimeoutError()
    receipt = tmp_path / "receipt.json"
    with pytest.raises(TimeoutError):
        publish_release(stage, api=api, receipt=receipt)
    api.repo_info.return_value = SimpleNamespace(sha="empty")
    api.list_repo_files.side_effect = lambda *a, **k: (
        [".gitattributes"]
        if k["revision"] == "empty"
        else [*verify_release(stage)["files"], "release-files.json"]
    )
    commits = []

    def commit(**kwargs):
        assert kwargs["parent_commit"] == (commits[-1] if commits else "empty")
        value = "chunk" + str(len(commits))
        commits.append(value)
        return SimpleNamespace(oid=value)

    api.create_commit.side_effect = commit
    result = recover_empty_upload(stage, api=api, receipt=receipt, batch_size=5)
    assert result["state"] == "completed"
    assert result["commit_sha"] == commits[-1]
    assert len(commits) > 1


def test_new_large_release_uses_bounded_commits_and_publishes_card_last(selection, tmp_path):
    stage = tmp_path / "stage"
    stage_release(selection, stage)
    api = Mock()
    api.repo_info.return_value = SimpleNamespace(sha="empty")
    api.list_repo_files.side_effect = lambda *a, **k: (
        [".gitattributes"]
        if k["revision"] == "empty"
        else [*verify_release(stage)["files"], "release-files.json"]
    )
    api.create_commit.return_value = SimpleNamespace(oid="uploaded")
    result = publish_release(stage, api=api, receipt=tmp_path / "receipt.json", batch_size=5)
    assert result["state"] == "completed"
    api.upload_folder.assert_not_called()
    batches = api.create_commit.call_args_list
    assert all(len(c.kwargs["operations"]) <= 5 for c in batches)
    last_paths = [op.path_in_repo for op in batches[-1].kwargs["operations"]]
    assert last_paths[-2:] == ["README.md", "release-files.json"]
