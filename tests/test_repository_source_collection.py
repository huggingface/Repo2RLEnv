"""Mixed source directories and standalone modules retain narrow collection."""

import json
import shutil

import pytest

from repo2rlenv.pipelines.recipes.repository.export import export_repository_task
from repo2rlenv.pipelines.recipes.swe_smith.grade import validate_submission
from repo2rlenv.quality.loop.probe_audit import submission_files
from repo2rlenv.spec.recipe_options import PythonRepositoryProfile


@pytest.fixture
def source_tree(tmp_path, monkeypatch):
    base = tmp_path / "base"
    for name, content in {
        "lib/existing.py": "value = 1\n",
        "lib/new.py": "feature = 2\n",
        "example.py": "example = 3\n",
        "unrelated.py": "private_to_collection = 4\n",
        "README.md": "Package description",
        "tests/test_api.py": "private test fixture",
    }.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    monkeypatch.setattr("os.chown", lambda *args: None)
    return base


def export(base, destination, *, roots=None, example_baseline=None):
    return export_repository_task(
        base=base,
        defective={"lib/new.py": None, "example.py": example_baseline},
        reference={"lib/new.py": b"feature = 2\n", "example.py": b"example = 3\n"},
        options=PythonRepositoryProfile(
            source_paths=roots or ["lib", "example.py"], test_paths=["tests"]
        ),
        instruction="Add the feature and its example.",
        destination=destination,
        name="mixed-source",
        org="test",
        contrast={"FAIL_TO_PASS": ["new"], "PASS_TO_PASS": ["existing"]},
        metadata={"recipe": "tasksmith", "recipe_version": "1"},
    )


def test_mixed_roots_export_and_validate_absent_or_present_added_file(source_tree, tmp_path):
    from harbor.models.task.task import Task

    task = export(source_tree, tmp_path / "tasks")
    contract = json.loads((task / "tests/contract.json").read_text())
    assert contract["submitted_roots"] == ["lib"]
    assert set(contract["submitted_files"]) == {"lib/existing.py", "lib/new.py", "example.py"}
    assert set(contract["optional_files"]) == {"lib/new.py", "example.py"}
    assert [artifact.source for artifact in Task(task).config.artifacts] == [
        "/workspace/lib",
        "/workspace/example.py",
    ]
    assert not (task / "tests/source/example.py").exists()
    assert (task / "solution/reference/example.py").read_bytes() == b"example = 3\n"
    workspace = task / "environment/source"
    assert not (workspace / "example.py").exists()
    assert not (workspace / "tests").exists()
    validate_submission(workspace, contract)
    before = submission_files(workspace, contract)
    assert before["example.py"] is None
    assert "unrelated.py" not in before
    shutil.copyfile(task / "solution/reference/example.py", workspace / "example.py")
    (workspace / "lib/helper.py").write_text("helper = 5\n")
    validate_submission(workspace, contract)
    after = submission_files(workspace, contract)
    assert after["example.py"] is not None
    assert "lib/helper.py" in after


@pytest.mark.parametrize("invalid", ["directory", "oversized", "symlink", "dangling_symlink"])
def test_optional_standalone_source_must_be_bounded_regular_file(source_tree, tmp_path, invalid):
    task = export(source_tree, tmp_path / "tasks")
    contract = json.loads((task / "tests/contract.json").read_text())
    workspace = task / "environment/source"
    path = workspace / "example.py"
    if invalid == "directory":
        path.mkdir()
    elif invalid == "oversized":
        with path.open("wb") as handle:
            handle.truncate(8 * 1024 * 1024 + 1)
    else:
        path.symlink_to(source_tree / ("example.py" if invalid == "symlink" else "missing.py"))
    with pytest.raises(ValueError, match=r"symlink|bounded regular file"):
        validate_submission(workspace, contract)


def test_modified_standalone_source_remains_required(source_tree, tmp_path):
    task = export(source_tree, tmp_path / "tasks", example_baseline=b"example = 0\n")
    workspace = task / "environment/source"
    contract = json.loads((task / "tests/contract.json").read_text())
    assert (workspace / "example.py").read_bytes() == b"example = 0\n"
    validate_submission(workspace, contract)
    (workspace / "example.py").unlink()
    with pytest.raises(ValueError, match="bounded regular file"):
        validate_submission(workspace, contract)


@pytest.mark.parametrize(
    ("roots", "message"),
    [
        (["lib", "lib/existing.py", "example.py"], "overlap"),
        (["lib", "example.py", "tests"], "private assets"),
        (["lib", "example.py", "README.md"], "directories or Python files"),
        (["lib", "example.py", "absent.py"], "directories or Python files"),
    ],
)
def test_mixed_roots_preserve_export_boundaries(source_tree, tmp_path, roots, message):
    with pytest.raises(ValueError, match=message):
        export(source_tree, tmp_path / "tasks", roots=roots)


def test_absent_optional_file_cannot_hide_a_linked_parent(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "linked").symlink_to(tmp_path / "absent", target_is_directory=True)
    contract = {"submitted_files": ["linked/example.py"], "optional_files": ["linked/example.py"]}
    with pytest.raises(ValueError, match="symlink"):
        validate_submission(workspace, contract)
