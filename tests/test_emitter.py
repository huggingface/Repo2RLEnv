"""Harbor task emitter writes the right files."""

from __future__ import annotations

import tomllib
from pathlib import Path

from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task


def _make_task(name: str = "demo__repo-1") -> HarborTask:
    return HarborTask(
        name=name,
        org="myorg",
        description="example",
        instruction="# Issue\n\nfix the bug",
        oracle_diff="--- a/x.py\n+++ b/x.py\n@@\n-1\n+2\n",
        repo2env={
            "pipeline": "pr_diff",
            "pipeline_version": "0.1.0",
            "repo": "demo/repo",
        },
    )


def test_writes_full_directory(tmp_path: Path):
    task = _make_task()
    out = write_harbor_task(task, tmp_path)
    assert out == tmp_path / "demo__repo-1"
    assert (out / "task.toml").is_file()
    assert (out / "instruction.md").is_file()
    assert (out / "solution" / "patch.diff").is_file()


def test_task_toml_is_valid_toml_with_harbor_layout(tmp_path: Path):
    task = _make_task()
    out = write_harbor_task(task, tmp_path)
    data = tomllib.loads((out / "task.toml").read_text())
    assert data["version"] == "1.0"
    # Harbor requires task.name in `org/name` format — we emit the qualified form
    assert data["task"]["name"] == "myorg/demo__repo-1"
    # Directory name still uses the bench-friendly slug (filesystem-safe)
    assert out.name == "demo__repo-1"
    r2e = data["metadata"]["repo2env"]
    assert r2e["pipeline"] == "pr_diff"
    assert r2e["spec_version"] == "0.1.0"
    assert r2e["content_hash"].startswith("sha256:")
    assert "diff_similarity" in r2e["reward_kinds"]


def test_instruction_and_oracle_round_trip(tmp_path: Path):
    task = _make_task()
    out = write_harbor_task(task, tmp_path)
    assert (out / "instruction.md").read_text() == task.instruction
    assert (out / "solution" / "patch.diff").read_text() == task.oracle_diff


def test_solve_sh_emitted_and_executable(tmp_path: Path):
    """Harbor's oracle agent runs solve.sh in the container — must exist + be +x."""
    task = _make_task()
    out = write_harbor_task(task, tmp_path)
    solve = out / "solution" / "solve.sh"
    assert solve.is_file()
    assert solve.stat().st_mode & 0o111  # executable
    content = solve.read_text()
    assert content.startswith("#!/bin/bash")
    # Must reference patch.diff (the canonical oracle artifact)
    assert "patch.diff" in content
    assert "git apply" in content


def test_content_hash_is_deterministic(tmp_path: Path):
    a = write_harbor_task(_make_task("a"), tmp_path / "a")
    b = write_harbor_task(_make_task("b"), tmp_path / "b")
    da = tomllib.loads((a / "task.toml").read_text())
    db = tomllib.loads((b / "task.toml").read_text())
    # Same instruction + oracle ⇒ same content_hash, regardless of name.
    assert da["metadata"]["repo2env"]["content_hash"] == db["metadata"]["repo2env"]["content_hash"]


def test_writes_environment_and_test_script_when_provided(tmp_path: Path):
    """Sandbox-required tasks (pr_runtime) emit Dockerfile + test.sh."""
    task = _make_task("sandbox-task")
    task.environment_dockerfile = "FROM ubuntu:24.04\nWORKDIR /workspace\n"
    task.test_script = "#!/bin/bash\nset -e\npytest -x\n"
    out = write_harbor_task(task, tmp_path)

    assert (out / "environment" / "Dockerfile").is_file()
    assert (out / "tests" / "test.sh").is_file()
    assert (out / "environment" / "Dockerfile").read_text() == task.environment_dockerfile
    assert (out / "tests" / "test.sh").read_text() == task.test_script
    # test.sh must be executable so Harbor can run it directly
    assert (out / "tests" / "test.sh").stat().st_mode & 0o111

    # reward_kinds upgrades to test_execution primary when test_script is present
    data = tomllib.loads((out / "task.toml").read_text())
    assert data["metadata"]["repo2env"]["reward_kinds"] == ["test_execution", "diff_similarity"]


def test_omits_environment_and_test_script_when_absent(tmp_path: Path):
    """Lite tasks (pr_diff) don't write environment/ or tests/."""
    task = _make_task("lite-task")
    out = write_harbor_task(task, tmp_path)
    assert not (out / "environment").exists()
    assert not (out / "tests").exists()
