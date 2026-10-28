"""Regression cases for the concrete grading bypasses in #214 and #220.

Text parsing is not an isolation boundary; these tests cover failure retention
and missing-verifier handling, not arbitrary malicious code in the test process.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from repo2rlenv import log_parsers
from repo2rlenv.pipelines import _pr_runtime_verifier as standalone
from repo2rlenv.pipelines.pr_runtime import build_eval_script


@pytest.mark.parametrize(
    ("parser", "log", "name"),
    [
        ("parse_go_test", "--- FAIL: TestA (0s)\n--- PASS: TestA (0s)\n", "TestA"),
        ("parse_cargo_test", "test a ... FAILED\ntest a ... ok\n", "a"),
        ("parse_jest", "FAIL a.test.js\n  ✕ fails\n  ✓ fails\n", "a.test.js > fails"),
        ("parse_jest", "  Suite\n    1) fails\n    ✔ fails\n", "Suite > fails"),
        (
            "parse_jest",
            " RUN  v5.0.1 /repo\n × a.test.ts > fails\n ✓ a.test.ts > fails\n",
            "a.test.ts > fails",
        ),
    ],
)
def test_failed_status_cannot_be_overwritten(parser, log, name):
    for module in (log_parsers, standalone):
        assert getattr(module, parser)(log)[name] == "FAILED"


@pytest.mark.skipif(os.name == "nt", reason="Generated shell runs in Linux containers")
def test_runtime_verifier_crash_does_not_credit_successful_command(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "verifier.py").write_text("raise RuntimeError('broken verifier')\n")
    script = (
        build_eval_script(
            base_commit="a" * 40,
            test_patch="",
            test_cmds=["true"],
            fail_to_pass=["test_fix"],
        )
        .replace("/workspace", shlex.quote(str(repo)))
        .replace("/logs/verifier", shlex.quote(str(tmp_path / "logs")))
    )
    path = tests / "test.sh"
    path.write_text(script)
    result = subprocess.run(
        ["bash", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig")},
    )
    assert result.returncode == 0, result.stderr
    assert float((tmp_path / "logs/reward.txt").read_text()) == 0
    details = json.loads((tmp_path / "logs/reward-details.json").read_text())
    assert details["parse_status"] == "verifier_error"
    assert details["evaluation_status"] == "ungraded"
    assert details["eval_trustworthy"] is False


@pytest.mark.skipif(os.name == "nt", reason="Emitted capture runs in Linux containers")
@pytest.mark.parametrize(
    "hook", ["diff.external", "diff.evil.textconv", "filter.evil.clean", "core.fsmonitor"]
)
def test_patch_capture_ignores_workspace_git_programs(tmp_path, hook, monkeypatch):
    from repo2rlenv.pipelines._pr_diff_capture import capture

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    source = repo / "answer.py"
    source.write_text("answer = 1\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    source.write_text("answer = 2\n")
    (repo / "new.py").write_text("new = True\n")
    (repo / ".gitattributes").write_text("*.py diff=evil filter=evil\n")
    marker = tmp_path / "executed"
    command = f"touch {shlex.quote(str(marker))}"
    git("config", hook, command)
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", command)
    # The workspace index is also untrusted; the private index must ignore it.
    (repo / ".git/index").write_bytes(b"invalid index")
    patch = capture(repo, base).decode()
    assert "+answer = 2" in patch
    assert "+new = True" in patch
    assert "diff --git a/new.py b/new.py" in patch
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="Generated shell runs in Linux containers")
def test_hidden_patch_restores_existing_tests_when_it_also_adds_a_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig")}

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    existing = repo / "tests/test_existing.py"
    existing.write_text("value = 1\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    existing.write_text("value = 2\n")
    added = repo / "tests/test_added.py"
    added.write_text("added = 3\n")
    git("add", ".")
    hidden = git("diff", "--cached", base) + "\n"
    existing.write_text("value = 999\n")
    added.write_text("added = 999\n")
    script = (
        build_eval_script(
            base_commit=base,
            test_patch=hidden,
            test_cmds=[
                "grep -qx 'value = 2' tests/test_existing.py",
                "grep -qx 'added = 3' tests/test_added.py",
            ],
        )
        .replace("/workspace", shlex.quote(str(repo)))
        .replace("/logs/verifier", shlex.quote(str(tmp_path / "logs")))
    )
    path = tmp_path / "test.sh"
    path.write_text(script)
    result = subprocess.run(
        ["bash", str(path)], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert float((tmp_path / "logs/reward.txt").read_text()) == 1
    assert existing.read_text() == "value = 1\n"
    assert not added.exists()
