"""Validation reruns the post-fix stage and drops tests whose status varies."""

from __future__ import annotations

import base64
import re

import pytest

from repo2rlenv.bootstrap.docker import ExecResult
from repo2rlenv.pipelines import pr_runtime_validate as validate

_PATCH = "--- a/src/m.py\n+++ b/src/m.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
_TEST_PATCH = "--- a/tests/test_m.py\n+++ b/tests/test_m.py\n@@ -1 +1,2 @@\n x = 1\n+y = 2\n"


def _log(**statuses: str) -> str:
    body = "".join(f"tests/test_m.py::{name} {status}\n" for name, status in statuses.items())
    return (
        "R2E_REBUILD_STATUS=ok\n"
        f"R2E_START_TEST_OUTPUT\n{body}R2E_END_TEST_OUTPUT\nR2E_TEST_EXIT_CODE=1\n"
    )


class _Sandbox:
    """Answers stage scripts in order: pre-fix once, then each post-fix run."""

    def __init__(self, pre: str, posts: list[str]) -> None:
        self.pre, self.posts, self.post_runs = pre, list(posts), 0
        self.staged: dict[str, str] = {}  # patches arrive as uploads, not in the script

    def _uploaded(self) -> str:
        return "".join(base64.b64decode(chunks).decode() for chunks in self.staged.values())

    def exec(self, script: str, timeout: int = 0) -> ExecResult:
        if script.startswith("rm -rf /opt/r2e-stage"):
            self.staged = {name: "" for name in re.findall(r": > (\S+)", script)}
        upload = re.fullmatch(r"printf %s (\S+) >> (\S+)", script)
        if upload:
            self.staged[upload.group(2)] += upload.group(1)
        if "R2E_START_TEST_OUTPUT" not in script:
            return ExecResult(0, "OK\n", "", 0.0)
        if "x = 2" in self._uploaded():  # the gold patch is staged: a post-fix stage
            self.post_runs += 1
            return ExecResult(1, self.posts.pop(0), "", 0.0)
        return ExecResult(1, self.pre, "", 0.0)


@pytest.fixture(autouse=True)
def _no_container_setup(monkeypatch):
    monkeypatch.setattr(validate, "_fetch_base_commit", lambda *a, **k: True)
    monkeypatch.setattr(validate, "_ensure_python", lambda *a, **k: True)


def _run(sandbox: _Sandbox) -> validate.ValidationOutcome:
    return validate.validate_pr(
        sandbox=sandbox,
        base_commit="a" * 40,
        patch=_PATCH,
        test_patch=_TEST_PATCH,
        test_cmds=["python -m pytest -rA tests/test_m.py"],
        language="python",
    )


def test_flaky_tests_helper():
    runs = [{"a": "PASSED", "b": "PASSED"}, {"a": "PASSED", "b": "FAILED"}, {"a": "PASSED"}]
    assert validate.flaky_tests(runs) == {"b"}
    assert validate.flaky_tests([{"a": "PASSED"}] * 3) == set()


def test_post_fix_stage_runs_three_times_and_drops_flaky_tests():
    pre = _log(test_fix="FAILED", test_flaky_fix="FAILED", test_keep="PASSED", test_wobble="PASSED")
    stable = {"test_fix": "PASSED", "test_keep": "PASSED"}
    posts = [
        _log(**stable, test_flaky_fix="PASSED", test_wobble="PASSED"),
        _log(**stable, test_flaky_fix="FAILED", test_wobble="PASSED"),
        _log(**stable, test_flaky_fix="PASSED", test_wobble="FAILED"),
    ]
    sandbox = _Sandbox(pre, posts)
    outcome = _run(sandbox)
    assert sandbox.post_runs == validate.POST_FIX_RUNS == 3
    assert outcome.status == "verified"
    assert outcome.fail_to_pass == ["tests/test_m.py::test_fix"]
    assert outcome.pass_to_pass == ["tests/test_m.py::test_keep"]
    assert outcome.flaky == ["tests/test_m.py::test_flaky_fix", "tests/test_m.py::test_wobble"]


def test_a_task_whose_only_fix_test_is_flaky_has_no_fail_to_pass():
    pre = _log(test_fix="FAILED")
    posts = [_log(test_fix="PASSED"), _log(test_fix="FAILED"), _log(test_fix="PASSED")]
    outcome = _run(_Sandbox(pre, posts))
    assert outcome.status == "failed" and outcome.fail_to_pass == []
    assert outcome.flaky == ["tests/test_m.py::test_fix"]


def test_a_gold_patch_over_the_argv_cap_is_uploaded_not_inlined():
    # 4,000 changed lines: the patch alone is ~180 KiB, over Linux's 128 KiB
    # per-argument cap (MAX_ARG_STRLEN) that `docker exec bash -lc` hits.
    old = "".join(f"-line_{i} = {i}\n" for i in range(4000))
    new = "".join(f"+line_{i} = {i} + 1  # x = 2\n" for i in range(4000))
    big = f"--- a/src/m.py\n+++ b/src/m.py\n@@ -1,4000 +1,4000 @@\n{old}{new}"
    assert len(big.encode()) > 128 * 1024

    class _Recording(_Sandbox):
        def __init__(self, *args) -> None:
            super().__init__(*args)
            self.sizes: list[int] = []

        def exec(self, script: str, timeout: int = 0) -> ExecResult:
            self.sizes.append(len(script.encode()))
            return super().exec(script, timeout)

    stable = {"test_fix": "PASSED"}
    sandbox = _Recording(_log(test_fix="FAILED"), [_log(**stable)] * 3)
    outcome = validate.validate_pr(
        sandbox=sandbox,
        base_commit="a" * 40,
        patch=big,
        test_patch=_TEST_PATCH,
        test_cmds=["python -m pytest -rA tests/test_m.py"],
        language="python",
    )
    assert max(sandbox.sizes) < 128 * 1024
    assert sandbox.post_runs == 3 and outcome.fail_to_pass == ["tests/test_m.py::test_fix"]
    assert big in sandbox._uploaded()


def test_an_image_root_cannot_trust_drops_the_task():
    # The launcher refuses to run tests when the test user could write a path
    # root later imports or executes from: no submission is gradable there.
    pre = "R2E_REBUILD_STATUS=ok\nR2E_TEST_EXIT_CODE=unsafe_image\n"
    outcome = _run(_Sandbox(pre, []))
    assert outcome.status == "failed"
    assert outcome.reason.startswith("unsafe image")
