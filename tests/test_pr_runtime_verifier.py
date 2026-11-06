"""Unit tests for the in-container graded F2P/P2P verifier.

Covers the 4 per-runner parsers (kept in lockstep with
repo2rlenv.log_parsers.*), the graded scoring + strict `resolved` bool,
P2P-regression penalty, the oracle=1.0 invariant, and the
exit-code fallback on unparseable logs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from repo2rlenv.pipelines import _pr_runtime_verifier as verifier
from repo2rlenv.pipelines._pr_runtime_verifier import (
    grade,
    main,
    parse_cargo_test,
    parse_go_test,
    parse_jest,
    parse_logs,
    parse_pytest,
)

# --- parsers -----------------------------------------------------------------


def test_parse_pytest_verbose_and_summary():
    log = (
        "tests/test_a.py::test_x PASSED  [ 50%]\n"
        "tests/test_a.py::test_y FAILED  [100%]\n"
        "FAILED tests/test_a.py::test_y - AssertionError: nope\n"
    )
    out = parse_pytest(log)
    assert out["tests/test_a.py::test_x"] == "PASSED"
    assert out["tests/test_a.py::test_y"] == "FAILED"


def test_parse_pytest_error_status():
    out = parse_pytest("ERROR tests/test_a.py::test_setup\n")
    assert out["tests/test_a.py::test_setup"] == "ERROR"


def test_parse_go_test():
    log = "=== RUN   TestA\n--- PASS: TestA (0.00s)\n--- FAIL: TestB (0.01s)\n"
    out = parse_go_test(log)
    assert out == {"TestA": "PASSED", "TestB": "FAILED"}


def test_parse_cargo_test():
    log = "test tests::a ... ok\ntest tests::b ... FAILED\ntest tests::c ... ignored\n"
    out = parse_cargo_test(log)
    assert out == {"tests::a": "PASSED", "tests::b": "FAILED", "tests::c": "SKIPPED"}


def test_parse_jest_qualified_names():
    log = "PASS  src/foo.test.ts\n  Foo\n    ✓ returns 200 (4 ms)\n    ✕ returns 500 (1 ms)\n"
    out = parse_jest(log)
    assert out["src/foo.test.ts > Foo > returns 200"] == "PASSED"
    assert out["src/foo.test.ts > Foo > returns 500"] == "FAILED"


def test_parse_logs_dispatch_by_runner():
    assert parse_logs("go", "--- PASS: T (0s)\n") == {"T": "PASSED"}
    assert parse_logs("unknown", "anything") == {}


# --- grading -----------------------------------------------------------------


def test_grade_oracle_full_resolution_is_one():
    """The invariant: all F2P pass + all P2P pass -> reward 1.0, resolved."""
    status = {"t_fix": "PASSED", "t_keep": "PASSED"}
    r = grade(["t_fix"], ["t_keep"], status)
    assert r["reward"] == 1.0
    assert r["resolved"] is True


def test_grade_partial_f2p_is_graded():
    status = {"f1": "PASSED", "f2": "FAILED", "keep": "PASSED"}
    r = grade(["f1", "f2"], ["keep"], status)
    assert r["f2p_rate"] == 0.5
    assert r["reward"] == 0.5  # p2p_rate == 1.0
    assert r["resolved"] is False


def test_grade_p2p_regression_penalizes():
    """Breaking a previously-passing test scales the reward down."""
    status = {"f1": "PASSED", "keep1": "PASSED", "keep2": "FAILED"}
    r = grade(["f1"], ["keep1", "keep2"], status)
    assert r["f2p_rate"] == 1.0
    assert r["p2p_rate"] == 0.5
    assert r["reward"] == 0.5
    assert r["resolved"] is False
    assert r["regressions"] == ["keep2"]


def test_grade_no_p2p_means_factor_one():
    r = grade(["f1"], [], {"f1": "PASSED"})
    assert r["p2p_rate"] == 1.0
    assert r["reward"] == 1.0
    assert r["resolved"] is True


def test_grade_missing_test_counts_as_not_passed():
    """An F2P test that didn't run at all is not credited."""
    r = grade(["f1", "f2"], [], {"f1": "PASSED"})  # f2 absent
    assert r["f2p_passed"] == 1
    assert r["f2p_rate"] == 0.5


def test_grade_zero_fix_zero_reward():
    r = grade(["f1"], ["keep"], {"f1": "FAILED", "keep": "PASSED"})
    assert r["reward"] == 0.0
    assert r["resolved"] is False


def test_grade_untracked_failure_keeps_tracked_resolved():
    """All F2P + P2P pass, but a test outside both sets FAILED. Tracked
    `resolved` stays True (gold-patch oracle invariant), reward stays 1.0, and
    the untracked failure is recorded so main() can block `command_resolved`.
    Audit P0 #1 (httpx-3412 case)."""
    r = grade(
        ["f1"],
        ["keep"],
        {"f1": "PASSED", "keep": "PASSED", "tests/other::cp1252": "FAILED"},
    )
    assert r["reward"] == 1.0  # tracked tests all green
    assert r["resolved"] is True  # tracked resolution preserved
    assert r["untracked_failed_count"] == 1
    assert r["untracked_failed"] == ["tests/other::cp1252"]


def test_grade_go_subtests_remain_failures_when_parent_passes():
    """Go subtest failures under a passing parent remain untracked."""
    r = grade(
        [],
        [],
        {
            "TestExample": "PASSED",
            "TestExample/subtest1": "FAILED",
            "TestExample/subtest2": "FAILED",
        },
        runner="go",
    )
    assert r["untracked_failed_count"] == 2
    assert r["untracked_failed"] == [
        "TestExample/subtest1",
        "TestExample/subtest2",
    ]


def test_grade_go_subtests_count_parent_failure_once():
    """Go subtests under a failed parent are represented by the parent failure."""
    r = grade(
        [],
        [],
        {
            "TestExample": "FAILED",
            "TestExample/subtest1": "FAILED",
            "TestExample/subtest2": "FAILED",
        },
        runner="go",
    )

    assert r["untracked_failed_count"] == 1
    assert r["untracked_failed"] == ["TestExample"]


def test_grade_no_untracked_failure_resolves():
    r = grade(["f1"], ["keep"], {"f1": "PASSED", "keep": "PASSED"})
    assert r["resolved"] is True
    assert r["untracked_failed_count"] == 0


# --- main() / IO -------------------------------------------------------------


def _write(p: Path, content: str) -> str:
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_main_writes_graded_reward(tmp_path: Path):
    log = _write(
        tmp_path / "out.log",
        "tests/t.py::t_fix PASSED\ntests/t.py::t_keep PASSED\n",
    )
    f2p = _write(tmp_path / "f2p.json", json.dumps(["tests/t.py::t_fix"]))
    p2p = _write(tmp_path / "p2p.json", json.dumps(["tests/t.py::t_keep"]))
    out_dir = tmp_path / "verifier"
    rc = main(
        [
            "--log",
            log,
            "--f2p",
            f2p,
            "--p2p",
            p2p,
            "--runner",
            "pytest",
            "--exit-code",
            "0",
            "--out-dir",
            str(out_dir),
        ]
    )
    assert rc == 0
    assert (out_dir / "reward.txt").read_text().strip() == "1.000000"
    assert not (out_dir / "reward.json").exists()
    breakdown = json.loads((out_dir / "reward-details.json").read_text())
    assert breakdown["resolved"] is True
    assert breakdown["command_resolved"] is True  # clean command, exit 0
    assert breakdown["parse_status"] == "ok"
    assert breakdown["exit_code"] == 0  # always recorded, not just in fallback


def test_main_go_subtest_failure_under_passing_parent_is_untracked(tmp_path: Path):
    """Go subtest failures under a passing parent remain untracked."""
    log = _write(
        tmp_path / "out.log",
        """--- PASS: TestTracked (0.01s)
    --- PASS: TestExample (0.01s)
        --- FAIL: TestExample/subtest1 (0.00s)
        --- FAIL: TestExample/subtest2 (0.00s)
    """,
    )
    f2p = _write(tmp_path / "f2p.json", json.dumps(["TestTracked"]))
    p2p = _write(tmp_path / "p2p.json", json.dumps([]))
    out_dir = tmp_path / "verifier"

    main(
        [
            "--log",
            log,
            "--f2p",
            f2p,
            "--p2p",
            p2p,
            "--runner",
            "go",
            "--exit-code",
            "0",
            "--out-dir",
            str(out_dir),
        ]
    )

    breakdown = json.loads((out_dir / "reward-details.json").read_text())
    assert breakdown["f2p_passed"] == 1
    assert breakdown["untracked_failed_count"] == 2
    assert breakdown["untracked_failed"] == [
        "TestExample/subtest1",
        "TestExample/subtest2",
    ]
    assert breakdown["command_resolved"] is False


def test_main_command_resolved_false_on_untracked_failure(tmp_path: Path):
    """All tracked tests pass (resolved True) but an untracked test failed and
    the command exited nonzero -> resolved True, command_resolved False."""
    log = _write(
        tmp_path / "out.log",
        "tests/t.py::t_fix PASSED\ntests/t.py::t_keep PASSED\ntests/t.py::t_flaky FAILED\n",
    )
    f2p = _write(tmp_path / "f2p.json", json.dumps(["tests/t.py::t_fix"]))
    p2p = _write(tmp_path / "p2p.json", json.dumps(["tests/t.py::t_keep"]))
    out_dir = tmp_path / "verifier"
    main(
        [
            "--log",
            log,
            "--f2p",
            f2p,
            "--p2p",
            p2p,
            "--runner",
            "pytest",
            "--exit-code",
            "1",
            "--out-dir",
            str(out_dir),
        ]
    )
    b = json.loads((out_dir / "reward-details.json").read_text())
    assert b["reward"] == 1.0  # tracked subset clean -> training reward 1.0
    assert b["resolved"] is True  # tracked resolution preserved
    assert b["command_resolved"] is False  # untracked failure + nonzero exit
    assert b["untracked_failed_count"] == 1


def test_main_unparseable_log_with_oracle_scores_zero(tmp_path: Path):
    """F2P was built by parsing this runner's output, so an unparseable log
    plus exit 0 means the agent silenced or reshaped the suite's output."""
    log = _write(tmp_path / "out.log", "garbage that no parser understands\n")
    f2p = _write(tmp_path / "f2p.json", json.dumps(["t_fix"]))
    p2p = _write(tmp_path / "p2p.json", json.dumps([]))
    out_dir = tmp_path / "verifier"
    main(
        [
            "--log",
            log,
            "--f2p",
            f2p,
            "--p2p",
            p2p,
            "--runner",
            "pytest",
            "--exit-code",
            "0",
            "--out-dir",
            str(out_dir),
        ]
    )
    breakdown = json.loads((out_dir / "reward-details.json").read_text())
    assert breakdown["parse_status"] == "fallback_exitcode"
    assert (out_dir / "reward.txt").read_text().strip() == "0.000000"


def test_main_unparseable_log_without_oracle_uses_exit_code(tmp_path: Path):
    """No declared F2P (e.g. --skip-validation): the exit code is the reward."""
    log = _write(tmp_path / "out.log", "garbage that no parser understands\n")
    f2p = _write(tmp_path / "f2p.json", json.dumps([]))
    p2p = _write(tmp_path / "p2p.json", json.dumps([]))
    out_dir = tmp_path / "verifier"
    main(["--log", log, "--f2p", f2p, "--p2p", p2p, "--exit-code", "0", "--out-dir", str(out_dir)])
    breakdown = json.loads((out_dir / "reward-details.json").read_text())
    assert breakdown["parse_status"] == "fallback_exitcode"
    assert (out_dir / "reward.txt").read_text().strip() == "1.000000"


def test_printed_pass_line_cannot_overwrite_a_failure():
    """Keep in lockstep with the package-side parser (test_log_parsers.py)."""
    log = (
        "tests/foo.py::test_x FAILED                                       [100%]\n"
        "FAILED tests/foo.py::test_x - AssertionError\n"
        "PASSED tests/foo.py::test_x\n"
    )
    assert parse_pytest(log)["tests/foo.py::test_x"] == "FAILED"


def test_main_fallback_not_resolved_when_oracle_declared(tmp_path: Path):
    """Unparseable log + declared F2P → reward may be exit-code-based, but
    `resolved` must be False (no per-test evidence). Audit P1."""
    log = _write(tmp_path / "out.log", "garbage no parser understands\n")
    f2p = _write(tmp_path / "f2p.json", json.dumps(["t_fix"]))  # declared oracle
    p2p = _write(tmp_path / "p2p.json", json.dumps([]))
    out_dir = tmp_path / "verifier"
    main(["--log", log, "--f2p", f2p, "--p2p", p2p, "--exit-code", "0", "--out-dir", str(out_dir)])
    b = json.loads((out_dir / "reward-details.json").read_text())
    assert b["parse_status"] == "fallback_exitcode"
    assert b["resolved"] is False  # cannot confirm F2P passed
    assert b["eval_trustworthy"] is False


def test_main_fallback_exit_nonzero_is_zero(tmp_path: Path):
    log = _write(tmp_path / "out.log", "garbage\n")
    f2p = _write(tmp_path / "f2p.json", json.dumps(["t_fix"]))
    p2p = _write(tmp_path / "p2p.json", json.dumps([]))
    out_dir = tmp_path / "verifier"
    main(["--log", log, "--f2p", f2p, "--p2p", p2p, "--exit-code", "1", "--out-dir", str(out_dir)])
    assert (out_dir / "reward.txt").read_text().strip() == "0.000000"


# --- logs the code under test can print -------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "t.py::a" + " " * 200_000 + "x",  # quadratic in the old verbose regex
        "t.py::a" + " " * 200_000 + "PASSEDx",
        "FAILED " + "[" * 200_000,  # quadratic in the old summary regex
        "PASSED a" + "[" * 100_000 + " - x",
    ],
)
def test_pathological_lines_parse_in_linear_time(line):
    import time

    from repo2rlenv import log_parsers

    log = "\n".join([line] * 5 + ["tests/t.py::test_a PASSED  [100%]"])
    started = time.monotonic()
    verifier_statuses = verifier.parse_logs("pytest", log)
    package_statuses = log_parsers.parse_logs(["pytest"], log)
    assert time.monotonic() - started < 2.0
    assert verifier_statuses["tests/t.py::test_a"] == "PASSED"
    assert package_statuses["tests/t.py::test_a"] == "PASSED"


# main's verbose-line regex: correct, but quadratic on crafted lines.
_OLD_VERBOSE = re.compile(
    r"^(?P<name>.+?(?:\[.*?\])?)\s+(?P<status>PASSED|FAILED|SKIPPED|ERROR)"
    rf"(?:\s+\(.*\))?(?:\s+(?:{verifier._PYTEST_PROGRESS}))?$"
)


def _old_verbose(line):
    match = _OLD_VERBOSE.match(line)
    return (match.group("name"), match.group("status")) if match else None


@pytest.mark.parametrize(
    "line",
    [
        "tests/a.py::T SKIPPED (needs PASSED x) FAILED (x)",
        "tests/a.py::t FAILED (x) PASSED (y)",
        "tests/a.py::t[a] PASSED] PASSED",
        "tests/a.py::t[ PASSED ] PASSED [ 50%]",
        "t.py::f[x (y)] SKIPPED (a) [ 1%]",
        "tests/a.py::t PASSED (x",
    ],
)
def test_verbose_lines_parse_like_mains_regex(line):
    from repo2rlenv.log_parsers import pytest_parser

    assert verifier._verbose_match(line) == _old_verbose(line)
    assert pytest_parser._verbose_match(line) == _old_verbose(line)


def test_verbose_matcher_fuzzes_equal_to_mains_regex():
    import random

    from repo2rlenv.log_parsers import pytest_parser

    pieces = ["a", "[", "]", " ", "\t", "-", "::", "PASSED", "FAILED", "SKIPPED", "ERROR"]
    pieces += ["(r)", "(", ")", "[ 50%]", "[3/10]", "0.01s", "1m 2s", "x]", "\u00a0"]
    rng = random.Random(0)
    for _ in range(20_000):
        line = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 10)))
        expected = _old_verbose(line)
        assert verifier._verbose_match(line) == expected, line
        assert pytest_parser._verbose_match(line) == expected, line


def test_summary_names_keep_bracketed_dashes():
    assert verifier._summary_name("tests/a.py::t[a - b] - AssertionError") == "tests/a.py::t[a - b]"
    assert verifier._summary_name("tests/a.py::t - boom [1] == [2]") == "tests/a.py::t"
    assert verifier._summary_name("tests/a.py::t") == "tests/a.py::t"


def test_unreadable_grader_input_is_its_own_exit_status(tmp_path):
    log = tmp_path / "log"
    log.write_text("tests/t.py::test_a PASSED\n")
    code = verifier.main(
        [
            "--log",
            str(log),
            "--f2p",
            str(tmp_path / "missing.json"),
            "--test-cmds",
            "pytest",
            "--out-dir",
            str(tmp_path / "out"),
        ]
    )
    assert code == verifier.EXIT_GRADER_INPUT
    assert not (tmp_path / "out" / "reward.txt").exists()


def test_cpu_limit_kills_a_stalled_parse(tmp_path):
    # The verifier runs as a script: a parse stalled past the CPU limit dies,
    # and test.sh grades that 0 rather than waiting for Harbor's timeout.
    import subprocess
    import sys
    import textwrap

    probe = tmp_path / "probe.py"
    probe.write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(Path(verifier.__file__).parent)!r})
            import _pr_runtime_verifier as v
            v.CPU_LIMIT_SEC = 1
            v._limit_cpu()
            while True:
                pass
            """
        )
    )
    result = subprocess.run([sys.executable, str(probe)], capture_output=True, timeout=60)
    assert result.returncode != 0
