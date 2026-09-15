"""Added-file packaging and collection contracts, without target execution."""

import hashlib
import json
import subprocess

import pytest

from repo2rlenv.pipelines.recipes.repository.export import export_repository_task
from repo2rlenv.pipelines.recipes.swe_smith.grade import validate_submission
from repo2rlenv.spec.recipe_options import PythonRepositoryProfile
from repo2rlenv.tasksmith import source


def test_intake_preserves_added_operations_and_identity(monkeypatch):
    rows = [{"filename": "lib/new.py", "status": "added", "additions": 1, "deletions": 0}]
    pull = {
        "merged_at": "yes",
        "changed_files": 1,
        "base": {"sha": "b", "repo": {"private": False}},
        "head": {"sha": "h"},
        "title": "Add behavior",
        "body": "",
    }
    patch = "diff --git a/lib/new.py b/lib/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/lib/new.py\n@@ -0,0 +1 @@\n+value = 1\n"
    monkeypatch.setattr(
        source, "_run_gh", lambda args: json.dumps([rows] if "/files?" in args[1] else pull)
    )
    monkeypatch.setattr(source, "fetch_pr_diff", lambda *args: patch)
    url = "https://github.com/example/lib/pull/1"
    result = source.resolve_pr(url)
    assert result["source_operations"] == [{"path": "lib/new.py", "operation": "added"}]
    identity = {"url": url, "head": "h", "source_diff": patch}
    assert (
        result["id"]
        == hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    )
    pull["changed_files"] = 2
    with pytest.raises(ValueError, match="inventory is incomplete"):
        source.resolve_pr(url)


def test_added_modules_are_absent_and_new_helpers_can_be_submitted(tmp_path, monkeypatch):
    base = tmp_path / "base"
    for name, text in {
        "lib/__init__.py": "",
        "lib/new/model.py": "value = 1\n",
        "lib/data.txt": "fixed",
        "tests/test_api.py": "private",
    }.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    task = export_repository_task(
        base=base,
        defective={"lib/new/model.py": None},
        reference={"lib/new/model.py": b"value = 1\n"},
        options=PythonRepositoryProfile(source_paths=["lib"], test_paths=["tests"]),
        instruction="Add the requested model API.",
        destination=tmp_path / "tasks",
        name="added-source",
        org="test",
        contrast={"FAIL_TO_PASS": ["feature"], "PASS_TO_PASS": ["adjacent"]},
        metadata={"recipe": "tasksmith", "recipe_version": "1"},
    )
    assert not (task / "environment/source/lib/new/model.py").exists()
    assert not (task / "tests/source/lib/new/model.py").exists()
    assert (task / "solution/reference/lib/new/model.py").read_bytes() == b"value = 1\n"
    assert "mkdir -p -- /workspace/lib/new" in (task / "solution/solve.sh").read_text()
    from harbor.models.task.task import Task

    assert Task(task).config.artifacts[0].source == "/workspace/lib"
    contract = json.loads((task / "tests/contract.json").read_text())
    workspace = task / "environment/source"
    monkeypatch.setattr("os.chown", lambda *args: None)
    # Missing new APIs are left to behavioral assertions, not a collection exception.
    validate_submission(workspace, contract)
    (workspace / "lib/helper.py").write_text("value = 1\n")
    validate_submission(workspace, contract)
    (workspace / "lib/data.txt").write_text("changed")
    with pytest.raises(ValueError, match="must remain unchanged"):
        validate_submission(workspace, contract)
    (workspace / "lib/data.txt").write_text("fixed")
    (workspace / "lib/helper.py").unlink()
    (workspace / "lib/helper.py").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symlink"):
        validate_submission(workspace, contract)


def test_contrast_removes_added_file_before_remote_pytest(tmp_path, monkeypatch):
    from repo2rlenv.execution import python_repository as module

    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        text = json.dumps([{"State": {"ExitCode": 1}}]) if argv[:2] == ["docker", "inspect"] else ""
        if argv[:2] == ["docker", "cp"] and argv[2].endswith(":/tmp/results.xml"):
            from pathlib import Path

            Path(argv[3]).write_text(
                '<testsuite><testcase classname="api" name="new"><failure>missing</failure></testcase><testcase classname="api" name="old"/></testsuite>'
            )
        return subprocess.CompletedProcess(argv, 0, text, "")

    monkeypatch.setattr(module, "_run", run)
    result = module.test_image(
        "merged",
        PythonRepositoryProfile(source_paths=["lib"], test_paths=["tests"]),
        tmp_path / "run",
        removals=("lib/new.py",),
    )
    assert result.returncode == 1
    create = next(cmd for cmd in calls if cmd[:2] == ["docker", "create"])
    assert create[-3:-1] == ["sh", "-c"]
    assert create[-1].startswith("rm -f -- /workspace/lib/new.py && exec python -m pytest")
    assert calls[-1][:3] == ["docker", "rm", "-f"]
