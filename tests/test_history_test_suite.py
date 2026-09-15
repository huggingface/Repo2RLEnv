import json
import os
import subprocess
import sys

from repo2rlenv.pipelines.recipes.history.test_suite import stage_tests
from repo2rlenv.pipelines.recipes.repository.export import export_repository_task
from repo2rlenv.spec.recipe_options import PythonRepositoryProfile


def test_selected_tests_keep_relative_imports_and_helpers_private(tmp_path):
    head, old = tmp_path / "head", tmp_path / "old"
    (head / "tests").mkdir(parents=True)
    (old / "lib").mkdir(parents=True)
    (old / "lib/core.py").write_text("value = 0\n")
    (head / "tests/__init__.py").write_text("from .helpers import expected\n")
    (head / "tests/helpers.py").write_text("expected = 42\n")
    (head / "tests/test_selected.py").write_text(
        "from . import expected\ndef test_selected():\n    assert expected == 42\n"
    )
    (head / "tests/test_unselected.py").write_text("raise RuntimeError('not selected')\n")
    targets = stage_tests(head, old, ["tests/test_selected.py"], ["tests"])
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *targets, "-q", "-p", "no:cacheprovider"],
        cwd=old,
        env={
            **os.environ,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    options = PythonRepositoryProfile(
        source_paths=["lib"],
        test_paths=targets,
        private_test_paths=["r2e_tests"],
        pytest_args=["-o", "addopts="],
    )
    task = export_repository_task(
        base=old,
        defective={"lib/core.py": b"value = 0\n"},
        reference={"lib/core.py": b"value = 42\n"},
        options=options,
        instruction="Restore the expected value.",
        destination=tmp_path / "tasks",
        name="history-fixture",
        org="tests",
        contrast={"FAIL_TO_PASS": ["test_selected"], "PASS_TO_PASS": []},
        metadata={"recipe": "r2e_gym", "recipe_version": "1"},
    )
    assert not (task / "environment/source/r2e_tests").exists()
    assert (task / "tests/source/r2e_tests/tests/helpers.py").read_text() == "expected = 42\n"
    contract = json.loads((task / "tests/contract.json").read_text())
    assert contract["test_paths"] == targets
    assert contract["pytest_args"] == ["-o", "addopts="]
