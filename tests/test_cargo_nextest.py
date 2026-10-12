"""cargo-nextest status lines must produce a runtime oracle (#172)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from repo2rlenv.log_parsers import parse_logs
from repo2rlenv.log_parsers.cargo_parser import parse_cargo_nextest
from repo2rlenv.pipelines import _pr_runtime_verifier as verifier
from repo2rlenv.pipelines.pr_runtime import normalize_test_cmds_for_runtime


@pytest.fixture(
    params=[parse_cargo_nextest, verifier.parse_cargo_nextest], ids=["canonical", "standalone"]
)
def parser(request):
    return request.param


# cargo-nextest 0.9.146, one passing and one failing test (from #172).
_ISSUE_LOG = """\
    Starting 2 tests across 1 binary (1 test skipped)
        PASS [   0.013s] (1/2) nextest-repro tests::accepts_even
        FAIL [   0.013s] (2/2) nextest-repro tests::rejects_odd
"""

# `--retries 2` output from nextest's docs (site/src/outputs/flaky-output.ansi),
# plus a test that fails every attempt. The captured libtest output under a
# failed attempt is indented and must not be read as a result.
_RETRY_LOG = """\
    Starting 2 tests across 2 binaries (11 skipped)
  TRY 1 FAIL [   0.003s] fixture-project::basic test_flaky_mod_2
  stdout ---

    running 1 test
    test test_flaky_mod_2 ... FAILED

    test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 17 filtered out

  TRY 2 PASS [   0.002s] fixture-project::basic test_flaky_mod_2
  TRY 1 FAIL [   0.003s] fixture-project::basic test_always_fails
  TRY 2 FAIL [   0.003s] fixture-project::basic test_always_fails
  TRY 3 FAIL [   0.003s] fixture-project::basic test_always_fails
------------
     Summary [   0.021s] 2 tests run: 1 passed (1 flaky), 1 failed, 25 skipped
   FLAKY 2/3 [   0.002s] fixture-project::basic test_flaky_mod_2
  TRY 3 FAIL [   0.003s] fixture-project::basic test_always_fails
"""

# Slow, timeout and leak lines from nextest's docs outputs.
_SLOW_TIMEOUT_LEAK_LOG = """\
        SLOW [> 60.000s] fixture-project::basic tests::test_slow
        PASS [  90.001s] fixture-project::basic tests::test_slow
        SLOW [>  1.000s] fixture-project::basic tests::test_slow_timeout
 TERMINATING [>  2.000s] fixture-project::basic tests::test_slow_timeout
     TIMEOUT [   2.001s] fixture-project::basic tests::test_slow_timeout
        LEAK [   0.103s] fixture-project::basic test_subprocess_doesnt_exit
   LEAK-FAIL [   0.104s] fixture-project::basic test_subprocess_doesnt_exit_leak_fail
     SIGSEGV [   0.010s] fixture-project::basic test_segfault
        SKIP [         ] (───) fixture-project::basic test_ignored
        SKIP [         ] fixture-project::other test_ignored
     Summary [  90.002s] 6 tests run: 2 passed (1 slow), 4 failed, 19 skipped
        SLOW [  90.001s] fixture-project::basic tests::test_slow
"""


def test_issue_log(parser):
    assert parser(_ISSUE_LOG) == {
        "nextest-repro tests::accepts_even": "PASSED",
        "nextest-repro tests::rejects_odd": "FAILED",
    }


def test_retries_last_result_wins(parser):
    assert parser(_RETRY_LOG) == {
        "fixture-project::basic test_flaky_mod_2": "PASSED",
        "fixture-project::basic test_always_fails": "FAILED",
    }


def test_slow_timeout_and_leak(parser):
    assert parser(_SLOW_TIMEOUT_LEAK_LOG) == {
        "fixture-project::basic tests::test_slow": "PASSED",
        "fixture-project::basic tests::test_slow_timeout": "FAILED",
        "fixture-project::basic test_subprocess_doesnt_exit": "PASSED",
        "fixture-project::basic test_subprocess_doesnt_exit_leak_fail": "FAILED",
        "fixture-project::basic test_segfault": "FAILED",
        "fixture-project::basic test_ignored": "SKIPPED",
        "fixture-project::other test_ignored": "SKIPPED",
    }


def test_colored_and_stress_lines(parser):
    log = (
        "        \x1b[32;1mPASS\x1b[0m [   0.006s] [1/3] nextest-filtering::match test_expr_deps\n"
        "        \x1b[31;1mFAIL\x1b[0m [   0.006s] [2/3] nextest-filtering::match test_expr_deps\n"
    )
    assert parser(log) == {"nextest-filtering::match test_expr_deps": "FAILED"}


def test_same_test_path_in_two_binaries_stays_distinct(parser):
    log = (
        "        PASS [   0.001s] my-crate tests::it_works\n"
        "        FAIL [   0.001s] my-crate::integration tests::it_works\n"
    )
    assert parser(log) == {
        "my-crate tests::it_works": "PASSED",
        "my-crate::integration tests::it_works": "FAILED",
    }


@pytest.mark.parametrize(
    "cmd", ["cargo nextest run", "cargo nextest run --workspace", "cd crate && cargo nextest run"]
)
def test_nextest_is_detected(cmd):
    assert verifier._detect_runner(cmd) == "nextest"
    assert parse_logs([cmd], _ISSUE_LOG, language="rust") == parse_cargo_nextest(_ISSUE_LOG)


def test_cargo_test_still_uses_libtest_parser():
    assert verifier._detect_runner("cargo test --workspace") == "cargo"
    log = "test tests::adds ... ok\n"
    assert parse_logs(["cargo test"], log) == {"tests::adds": "PASSED"}


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        (
            "cargo nextest run 2>&1 | tail -50",
            "cargo nextest run --status-level pass --color never",
        ),
        (
            "cargo nextest run --workspace -- --include-ignored",
            "cargo nextest run --status-level pass --color never --workspace -- --include-ignored",
        ),
        (
            "cargo nextest run --status-level fail --color always",
            "cargo nextest run --status-level pass --color always",
        ),
        (
            "cargo nextest run --status-level=slow",
            "cargo nextest run --status-level pass --color never",
        ),
        (
            "cargo nextest run --status-level all",
            "cargo nextest run --color never --status-level all",
        ),
    ],
)
def test_normalize_shows_pass_lines(cmd, expected):
    assert normalize_test_cmds_for_runtime([cmd]) == [expected]


def test_standalone_verifier_grades_nextest(tmp_path: Path):
    before = parse_cargo_nextest(_ISSUE_LOG)
    after = _ISSUE_LOG.replace("FAIL [", "PASS [")
    f2p = [n for n, s in before.items() if s == "FAILED"]
    p2p = [n for n, s in before.items() if s == "PASSED"]
    assert f2p == ["nextest-repro tests::rejects_odd"]

    # Run the verifier in isolation, auto-detecting the runner from --test-cmds.
    standalone = tmp_path / "verifier.py"
    standalone.write_text(Path(verifier.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "out.log").write_text(after, encoding="utf-8")
    (tmp_path / "f2p.json").write_text(json.dumps(f2p), encoding="utf-8")
    (tmp_path / "p2p.json").write_text(json.dumps(p2p), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(standalone),
            "--log",
            "out.log",
            "--f2p",
            "f2p.json",
            "--p2p",
            "p2p.json",
            "--test-cmds",
            "cargo nextest run --status-level pass --color never",
            "--exit-code",
            "0",
            "--out-dir",
            "rewards",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    details = json.loads((tmp_path / "rewards/reward-details.json").read_text())
    assert details["reward"] == 1.0
    assert details["resolved"] is True
