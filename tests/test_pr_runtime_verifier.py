"""Unit tests for the in-container graded F2P/P2P verifier.

Covers the 4 per-runner parsers (kept in lockstep with
repo2rlenv.log_parsers.*), the graded scoring + strict `resolved` bool,
P2P-regression penalty, the oracle=1.0 invariant, and the
exit-code fallback on unparseable logs.
"""

from __future__ import annotations

import json
from pathlib import Path

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
    breakdown = json.loads((out_dir / "reward.json").read_text())
    assert breakdown["resolved"] is True
    assert breakdown["parse_status"] == "ok"


def test_main_falls_back_to_exit_code_on_unparseable_log(tmp_path: Path):
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
    breakdown = json.loads((out_dir / "reward.json").read_text())
    assert breakdown["parse_status"] == "fallback_exitcode"
    assert (out_dir / "reward.txt").read_text().strip() == "1.000000"


def test_main_fallback_exit_nonzero_is_zero(tmp_path: Path):
    log = _write(tmp_path / "out.log", "garbage\n")
    f2p = _write(tmp_path / "f2p.json", json.dumps(["t_fix"]))
    p2p = _write(tmp_path / "p2p.json", json.dumps([]))
    out_dir = tmp_path / "verifier"
    main(["--log", log, "--f2p", f2p, "--p2p", p2p, "--exit-code", "1", "--out-dir", str(out_dir)])
    assert (out_dir / "reward.txt").read_text().strip() == "0.000000"
