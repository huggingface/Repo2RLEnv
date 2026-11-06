"""Regression cases for the concrete grading bypasses in #214 and #220.

Text parsing is not an isolation boundary; these tests cover failure retention
and missing-verifier handling, not arbitrary malicious code in the test process.
"""

from __future__ import annotations

import os
import shlex
import subprocess

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


def test_runtime_verifier_crash_does_not_credit_a_successful_command():
    # The separate verifier's script: a verifier crash on the tests' output is
    # a graded 0, never a credited exit code.
    script = build_eval_script(
        base_commit="a" * 40, test_patch="", test_cmds=["true"], fail_to_pass=["test_fix"]
    )
    tail = script[script.index('"$SCRIPT_DIR/verifier.py"') :]
    assert "r2e_finish graded log_unparseable" in tail
    assert 'echo "1.0"' not in tail
