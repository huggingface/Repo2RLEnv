"""Inert editor files must not suppress behavioral tests or bypass source guards."""

import hashlib
import os
import shutil

import pytest

from repo2rlenv.pipelines.recipes.swe_smith.grade import validate_submission


@pytest.fixture
def submission(tmp_path, monkeypatch):
    workspace = tmp_path / "private"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src/model.py").write_text("value = 1\n")
    monkeypatch.setattr("os.chown", lambda *args: None)
    contract = {"submitted_files": ["src/model.py"], "submitted_roots": ["src"]}
    return workspace, contract


def test_removes_only_private_copy_backups_and_keeps_python_sources(submission, tmp_path):
    workspace, contract = submission
    for name in ("model.py.bak", "model.py~"):
        (workspace / "src" / name).write_text("value = 0\n")
    (workspace / "src/new_helper.py").write_text("helper = 2\n")
    original = tmp_path / "archived_submission"
    shutil.copytree(workspace, original)

    validate_submission(workspace, contract)

    assert sorted(p.name for p in (workspace / "src").iterdir()) == ["model.py", "new_helper.py"]
    for name in ("model.py", "new_helper.py"):
        assert (workspace / "src" / name).read_bytes() == (original / "src" / name).read_bytes()
    assert (original / "src/model.py.bak").read_text() == "value = 0\n"
    assert (original / "src/model.py~").read_text() == "value = 0\n"
    validate_submission(workspace, contract)  # An already cleaned staging copy remains valid.


@pytest.mark.parametrize("name", ["model.bak", "model.py.orig", "model.py.bak.json", "config.json"])
def test_unknown_non_python_additions_still_reject_without_cleaning(submission, name):
    workspace, contract = submission
    backup = workspace / "src/model.py.bak"
    backup.write_text("previous\n")
    (workspace / "src" / name).write_text("unrecognized\n")
    with pytest.raises(ValueError, match="Only Python source files"):
        validate_submission(workspace, contract)
    assert backup.read_text() == "previous\n"


@pytest.mark.parametrize("kind", ["symlink", "dangling_symlink", "oversized", "fifo"])
def test_backup_paths_keep_regular_file_and_symlink_guards(submission, tmp_path, kind):
    workspace, contract = submission
    backup = workspace / "src/model.py.bak"
    outside = tmp_path / "outside.py"
    outside.write_text("outside\n")
    if kind in {"symlink", "dangling_symlink"}:
        backup.symlink_to(outside if kind == "symlink" else tmp_path / "missing.py")
    elif kind == "oversized":
        with backup.open("wb") as stream:
            stream.truncate(8 * 1024 * 1024 + 1)
    else:
        os.mkfifo(backup)
    with pytest.raises(ValueError, match=r"symlink|bounded regular file"):
        validate_submission(workspace, contract)
    assert backup.exists() or backup.is_symlink()
    assert outside.read_text() == "outside\n"


def test_backup_does_not_substitute_for_a_required_source_file(submission):
    workspace, contract = submission
    source = workspace / "src/model.py"
    backup = source.with_suffix(".py.bak")
    source.rename(backup)
    with pytest.raises(ValueError, match="bounded regular file"):
        validate_submission(workspace, contract)
    assert backup.read_text() == "value = 1\n"


def test_declared_immutable_backup_is_preserved_and_checked(submission):
    workspace, contract = submission
    backup = workspace / "src/model.py.bak"
    backup.write_text("shipped asset\n")
    contract["immutable_assets"] = {
        "src/model.py.bak": hashlib.sha256(backup.read_bytes()).hexdigest()
    }
    validate_submission(workspace, contract)
    assert backup.read_text() == "shipped asset\n"
    backup.write_text("changed\n")
    with pytest.raises(ValueError, match="must remain unchanged"):
        validate_submission(workspace, contract)
    assert backup.read_text() == "changed\n"


def test_explicit_submission_is_not_reclassified_as_a_discardable_backup(submission):
    workspace, contract = submission
    backup = workspace / "src/model.py.bak"
    backup.write_text("declared\n")
    contract["submitted_files"].append("src/model.py.bak")
    with pytest.raises(ValueError, match="Only Python source files"):
        validate_submission(workspace, contract)
    assert backup.read_text() == "declared\n"


def test_linked_root_parent_cannot_delete_external_backup(submission, tmp_path):
    workspace, contract = submission
    outside = tmp_path / "external"
    (outside / "src").mkdir(parents=True)
    backup = outside / "src/model.py.bak"
    backup.write_text("external backup\n")
    (workspace / "linked").symlink_to(outside, target_is_directory=True)
    contract["submitted_roots"].append("linked/src")
    with pytest.raises(ValueError, match="symlink"):
        validate_submission(workspace, contract)
    assert backup.read_text() == "external backup\n"
