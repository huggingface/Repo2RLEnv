"""The worked migration preserves old evidence and creates a distinct task revision."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pytest
import tomli_w

from repo2rlenv.quality.loop.artifacts import refresh_identity, task_identity
from tests import test_quality_reviewer as fixtures

task = fixtures.task
EXAMPLE = Path(__file__).parents[1] / "examples/tutorials/verifier-isolation/migrate.py"
spec = importlib.util.spec_from_file_location("isolation_example", EXAMPLE)
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


def test_rejects_unrelated_task_without_creating_output(task, tmp_path):
    destination = tmp_path / "revision"
    with pytest.raises(ValueError, match="only migrates"):
        example.migrate(task, destination)
    assert not destination.exists()


def test_migration_preserves_source_and_invalidates_old_identity(task, tmp_path):
    config = tomllib.loads((task / "task.toml").read_text())
    config["task"] = {"name": "repo2rlenv/tasksmith-3d762a2acfee"}
    (task / "task.toml").write_text(tomli_w.dumps(config))
    (task / "tests/Dockerfile").write_text(
        "FROM python:3.12-slim\n"
        "COPY grade.py test_driver.py test_results.py contract.json test.sh /tests/\n"
        "RUN chmod 755 /tests && chmod 644 /tests/*\n"
    )
    for filename in ("grade.py", "test_driver.py", "test_results.py", "contract.json"):
        (task / "tests" / filename).write_text("# Existing trusted source\n")
    refresh_identity(task)
    original_hash = task_identity(task)
    original_oracle = (task / "solution/solve.sh").read_bytes()
    destination = tmp_path / "revision"
    example.migrate(task, destination)
    assert task_identity(task) == original_hash
    assert task_identity(destination) != original_hash
    assert (destination / "solution/solve.sh").read_bytes() == original_oracle
    assert "chmod 700 /tests" in (destination / "tests/Dockerfile").read_text()
    assert "test.sh /tests/" in (destination / "tests/Dockerfile").read_text()
    with pytest.raises(ValueError, match="new output"):
        example.migrate(task, destination)
