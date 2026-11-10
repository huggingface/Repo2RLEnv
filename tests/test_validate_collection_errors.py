"""Tests in a module that can't be imported at base still count as fail-to-pass."""

from __future__ import annotations

import base64
import re

import pytest

from repo2rlenv.bootstrap.docker import ExecResult
from repo2rlenv.pipelines import pr_runtime_validate as validate

_PATCH = "--- a/src/m.py\n+++ b/src/m.py\n@@ -1 +1,2 @@\n x = 1\n+y = 2\n"
_TEST_PATCH = (
    "--- a/tests/test_m.py\n+++ b/tests/test_m.py\n@@ -1 +1 @@\n"
    "-from m import x\n+from m import x, y\n"
)
_CMDS = ["python -m pytest -v tests/test_m.py tests/test_other.py"]

# pytest at base: tests/test_m.py imports `y`, which only the fix adds.
_COLLECTION_ERROR = (
    "ERROR tests/test_m.py - ImportError: cannot import name 'y' from 'm'\n"
    "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n"
)


def _log(body: str) -> str:
    return (
        "R2E_REBUILD_STATUS=ok\n"
        f"R2E_START_TEST_OUTPUT\n{body}R2E_END_TEST_OUTPUT\nR2E_TEST_EXIT_CODE=1\n"
    )


def _results(**statuses: str) -> str:
    return "".join(f"{name} {status}\n" for name, status in statuses.items())


class _Sandbox:
    """Answers the pre-fix stage with `pre` and every post-fix run with `post`."""

    def __init__(self, pre: str, post: str) -> None:
        self.pre, self.post = pre, post
        self.staged: dict[str, str] = {}
        self.scripts: dict[str, str] = {}

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
        stage = "post" if "+y = 2" in self._uploaded() else "pre"
        self.scripts.setdefault(stage, script)
        return ExecResult(1, self.post if stage == "post" else self.pre, "", 0.0)


@pytest.fixture(autouse=True)
def _no_container_setup(monkeypatch):
    monkeypatch.setattr(validate, "_fetch_base_commit", lambda *a, **k: True)
    monkeypatch.setattr(validate, "_ensure_python", lambda *a, **k: True)
    monkeypatch.setattr(validate, "_validate_mutants", lambda **k: None)


def _run(sandbox: _Sandbox) -> validate.ValidationOutcome:
    return validate.validate_pr(
        sandbox=sandbox,
        base_commit="a" * 40,
        patch=_PATCH,
        test_patch=_TEST_PATCH,
        test_cmds=_CMDS,
        language="python",
    )


def test_tests_in_a_module_that_fails_to_import_at_base_are_fail_to_pass():
    post = _results(**{"tests/test_m.py::test_x": "PASSED", "tests/test_m.py::test_y": "PASSED"})
    outcome = _run(_Sandbox(_log(_COLLECTION_ERROR), _log(post)))
    assert outcome.status == "verified"
    assert outcome.fail_to_pass == ["tests/test_m.py::test_x", "tests/test_m.py::test_y"]
    assert outcome.pass_to_pass == []


def test_other_files_keep_their_own_transitions():
    pre = _COLLECTION_ERROR + _results(
        **{"tests/test_other.py::test_fixed": "FAILED", "tests/test_other.py::test_kept": "PASSED"}
    )
    post = _results(
        **{
            "tests/test_m.py::test_y": "PASSED",
            "tests/test_other.py::test_fixed": "PASSED",
            "tests/test_other.py::test_kept": "PASSED",
            # Absent at base, but its file collected fine there: not evidence of a fix.
            "tests/test_other.py::test_unseen": "PASSED",
        }
    )
    outcome = _run(_Sandbox(_log(pre), _log(post)))
    assert outcome.fail_to_pass == ["tests/test_m.py::test_y", "tests/test_other.py::test_fixed"]
    assert outcome.pass_to_pass == ["tests/test_other.py::test_kept"]


def test_a_test_still_failing_after_the_fix_is_not_fail_to_pass():
    post = _results(**{"tests/test_m.py::test_x": "PASSED", "tests/test_m.py::test_y": "FAILED"})
    outcome = _run(_Sandbox(_log(_COLLECTION_ERROR), _log(post)))
    assert outcome.fail_to_pass == ["tests/test_m.py::test_x"]


def test_only_the_pre_fix_stage_keeps_collecting_after_an_import_error():
    post = _results(**{"tests/test_m.py::test_y": "PASSED"})
    sandbox = _Sandbox(_log(_COLLECTION_ERROR), _log(post))
    _run(sandbox)
    assert "--continue-on-collection-errors" in sandbox.scripts["pre"]
    assert "--continue-on-collection-errors" not in sandbox.scripts["post"]
