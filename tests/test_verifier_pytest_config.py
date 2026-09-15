"""The isolated test runner must not require optional repository report plugins."""

import os
import subprocess
import sys
from pathlib import Path

from repo2rlenv.pipelines.recipes.swe_smith import test_driver


def test_selected_test_runs_without_repository_coverage_plugin(tmp_path):
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\naddopts = --cov=some_package --cov-report=term-missing\n"
    )
    test = tmp_path / "test_example.py"
    test.write_text("def test_sum():\n    assert sum([2, 3]) == 5\n")
    result = subprocess.run(
        [sys.executable, str(Path(test_driver.__file__)), str(test), "-q"],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
