"""unittest and Django runs must be parsed by both parsers, not read as pytest."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from repo2rlenv.log_parsers import parse_logs
from repo2rlenv.log_parsers.unittest_parser import parse_unittest
from repo2rlenv.pipelines import _pr_runtime_verifier as verifier


@pytest.fixture(params=[parse_unittest, verifier.parse_unittest], ids=["canonical", "standalone"])
def parser(request):
    return request.param


# Real `python -m unittest -v` output (CPython 3.14): every status unittest can
# report, a test whose docstring pushes the status onto the next line, and a
# subtest failure reported under its parent.
_VERBOSE = (
    "test_add (tests.test_math.MathTests.test_add) ... ok\n"
    "test_broken (tests.test_math.MathTests.test_broken) ... FAIL\n"
    "test_error (tests.test_math.MathTests.test_error) ... ERROR\n"
    "test_expected_failure (tests.test_math.MathTests.test_expected_failure) ... expected failure\n"
    "test_skipped (tests.test_math.MathTests.test_skipped) ... skipped 'later'\n"
    "test_subtests (tests.test_math.MathTests.test_subtests) ... \n"
    "  test_subtests (tests.test_math.MathTests.test_subtests) (i=2) ... FAIL\n"
    "test_with_docstring (tests.test_math.MathTests.test_with_docstring)\n"
    "Adds two negatives. ... ok\n"
    "test_subtests_pass (tests.test_math.MoreTests.test_subtests_pass) ... ok\n"
    "test_unexpected_success (tests.test_math.MoreTests.test_unexpected_success) ... unexpected success\n"
    "\n"
    "======================================================================\n"
    "ERROR: test_error (tests.test_math.MathTests.test_error)\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    '  File "/repo/tests/test_math.py", line 16, in test_error\n'
    '    raise RuntimeError("boom")\n'
    "RuntimeError: boom\n"
    "\n"
    "======================================================================\n"
    "FAIL: test_broken (tests.test_math.MathTests.test_broken)\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    '  File "/repo/tests/test_math.py", line 13, in test_broken\n'
    "    self.assertEqual(1 + 1, 3)\n"
    "    ~~~~~~~~~~~~~~~~^^^^^^^^^^\n"
    "AssertionError: 2 != 3\n"
    "\n"
    "======================================================================\n"
    "FAIL: test_subtests (tests.test_math.MathTests.test_subtests) (i=2)\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    '  File "/repo/tests/test_math.py", line 29, in test_subtests\n'
    "    self.assertEqual(i % 2, 1)\n"
    "    ~~~~~~~~~~~~~~~~^^^^^^^^^^\n"
    "AssertionError: 0 != 1\n"
    "\n"
    "======================================================================\n"
    "UNEXPECTED SUCCESS: test_unexpected_success (tests.test_math.MoreTests.test_unexpected_success)\n"
    "----------------------------------------------------------------------\n"
    "Ran 9 tests in 0.001s\n"
    "\n"
    "FAILED (failures=2, errors=1, skipped=1, expected failures=1, unexpected successes=1)\n"
)

# The same suite without -v: dots, then the failure blocks.
_PLAIN = (
    ".FExsF..u\n"
    "======================================================================\n"
    "ERROR: test_error (tests.test_math.MathTests.test_error)\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    '  File "/repo/tests/test_math.py", line 16, in test_error\n'
    '    raise RuntimeError("boom")\n'
    "RuntimeError: boom\n"
    "\n"
    "======================================================================\n"
    "FAIL: test_broken (tests.test_math.MathTests.test_broken)\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    '  File "/repo/tests/test_math.py", line 13, in test_broken\n'
    "    self.assertEqual(1 + 1, 3)\n"
    "    ~~~~~~~~~~~~~~~~^^^^^^^^^^\n"
    "AssertionError: 2 != 3\n"
    "\n"
    "======================================================================\n"
    "FAIL: test_subtests (tests.test_math.MathTests.test_subtests) (i=2)\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    '  File "/repo/tests/test_math.py", line 29, in test_subtests\n'
    "    self.assertEqual(i % 2, 1)\n"
    "    ~~~~~~~~~~~~~~~~^^^^^^^^^^\n"
    "AssertionError: 0 != 1\n"
    "\n"
    "======================================================================\n"
    "UNEXPECTED SUCCESS: test_unexpected_success (tests.test_math.MoreTests.test_unexpected_success)\n"
    "----------------------------------------------------------------------\n"
    "Ran 9 tests in 0.001s\n"
    "\n"
    "FAILED (failures=2, errors=1, skipped=1, expected failures=1, unexpected successes=1)\n"
)

# Real `manage.py test -v 2` output (Django 6.1.1), which uses TextTestRunner.
_DJANGO = (
    "test_add (demo.tests.MathTests.test_add) ... ok\n"
    "test_broken (demo.tests.MathTests.test_broken) ... FAIL\n"
    "test_with_docstring (demo.tests.MathTests.test_with_docstring)\n"
    "Adds two negatives. ... ok\n"
    "\n"
    "======================================================================\n"
    "FAIL: test_broken (demo.tests.MathTests.test_broken)\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    '  File "/repo/dj/demo/tests.py", line 13, in test_broken\n'
    "    self.assertEqual(1 + 1, 3)\n"
    "AssertionError: 2 != 3\n"
    "\n"
    "----------------------------------------------------------------------\n"
    "Ran 3 tests in 0.000s\n"
    "\n"
    "FAILED (failures=1)\n"
    "Found 3 test(s).\n"
    "Skipping setup of unused database(s): default.\n"
    "System check identified no issues (0 silenced).\n"
)


def test_every_status_is_recorded(parser):
    assert parser(_VERBOSE) == {
        "tests.test_math.MathTests.test_add": "PASSED",
        "tests.test_math.MathTests.test_broken": "FAILED",
        "tests.test_math.MathTests.test_error": "ERROR",
        "tests.test_math.MathTests.test_expected_failure": "PASSED",
        "tests.test_math.MathTests.test_skipped": "SKIPPED",
        "tests.test_math.MathTests.test_subtests": "FAILED",
        "tests.test_math.MathTests.test_with_docstring": "PASSED",
        "tests.test_math.MoreTests.test_subtests_pass": "PASSED",
        "tests.test_math.MoreTests.test_unexpected_success": "FAILED",
    }


def test_plain_run_records_only_the_failure_blocks(parser):
    assert parser(_PLAIN) == {
        "tests.test_math.MathTests.test_broken": "FAILED",
        "tests.test_math.MathTests.test_error": "ERROR",
        "tests.test_math.MathTests.test_subtests": "FAILED",
    }


def test_django_runner(parser):
    assert parser(_DJANGO) == {
        "demo.tests.MathTests.test_add": "PASSED",
        "demo.tests.MathTests.test_broken": "FAILED",
        "demo.tests.MathTests.test_with_docstring": "PASSED",
    }


def test_pre_311_name_layout(parser):
    # Before 3.11 the parentheses held only the class path.
    log = "test_add (tests.test_math.MathTests) ... ok\n"
    assert parser(log) == {"tests.test_math.MathTests.test_add": "PASSED"}


@pytest.mark.parametrize(
    ("tail", "status"),
    [
        ("ok", "PASSED"),
        ("FAIL", "FAILED"),
        ("ERROR", "ERROR"),
        ("expected failure", "PASSED"),
        ("unexpected success", "FAILED"),
        ("skipped 'needs network'", "SKIPPED"),
        ("skipped", "SKIPPED"),
    ],
)
def test_status_words(parser, tail, status):
    log = f"test_x (pkg.mod.Case.test_x) ... {tail}\n"
    assert parser(log) == {"pkg.mod.Case.test_x": status}


def test_docstring_status_only_binds_to_the_line_above(parser):
    # A name with no status, then an unrelated line, must record nothing.
    log = "test_x (pkg.mod.Case.test_x)\nsome other output ... ok\n"
    assert parser(log) == {"pkg.mod.Case.test_x": "PASSED"}
    log = "test_x (pkg.mod.Case.test_x)\n\nnoise\nmore noise ... ok\n"
    assert parser(log) == {}


def test_tracebacks_and_footers_are_ignored(parser):
    log = (
        "======================================================================\n"
        "FAIL: test_broken (pkg.mod.Case.test_broken)\n"
        "----------------------------------------------------------------------\n"
        "Traceback (most recent call last):\n"
        '  File "/repo/tests/test_math.py", line 13, in test_broken\n'
        "    self.assertEqual(1 + 1, 3)\n"
        "AssertionError: 2 != 3\n"
        "\n"
        "Ran 9 tests in 0.001s\n"
        "\n"
        "FAILED (failures=1, errors=1, skipped=1)\n"
    )
    assert parser(log) == {"pkg.mod.Case.test_broken": "FAILED"}


def test_dispatcher_routes_unittest_commands():
    for cmd in [
        "python -m unittest -v",
        "python -m unittest discover -s tests -v",
        "python manage.py test -v 2",
        "python tests/runtests.py --verbosity 2",
    ]:
        assert parse_logs([cmd], _VERBOSE), cmd
    # The standalone verifier detects the same commands from --test-cmds.
    assert verifier.parse_logs(verifier._detect_runner("python -m unittest -v"), _VERBOSE)


def test_unittest_footer_is_not_a_pytest_node():
    # A wrapper command lands in the pytest parser; its footer must not
    # become a test called `(errors=1)`.
    from repo2rlenv.log_parsers.pytest_parser import parse_pytest

    for parse in (parse_pytest, verifier.parse_pytest):
        assert parse(_VERBOSE) == {}
        assert parse("FAILED tests/test_a.py::test_x - AssertionError") == {
            "tests/test_a.py::test_x": "FAILED"
        }


def test_long_lines_do_not_stall_parsing():
    # Isolate the timeout so a backtracking regression cannot hang the suite.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from repo2rlenv.log_parsers.unittest_parser import parse_unittest\n"
            "from repo2rlenv.pipelines._pr_runtime_verifier import parse_unittest as standalone\n"
            "noise = ['t (' + 'a' * 200000 + ')', 'x' * 200000 + ' ... ok',\n"
            "         't (pkg.C.t)' + ' (i=1)' * 50000 + ' ... FAIL',\n"
            "         'FAIL: t (' + 'b.' * 100000 + 'C.t)']\n"
            "log = '\\n'.join(noise) + '\\ntest_x (pkg.mod.Case.test_x) ... ok\\n'\n"
            "for parser in (parse_unittest, standalone):\n"
            "    assert parser(log)['pkg.mod.Case.test_x'] == 'PASSED'\n",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# A real fix under unittest: `div` gains a zero check, and the test that
# covers it has a docstring, so its status is printed on the following line.
_PRE = (
    "test_divides (tests.test_calc.DivTests.test_divides) ... ok\n"
    "test_rejects_zero (tests.test_calc.DivTests.test_rejects_zero)\n"
    "Rejects a zero divisor. ... ERROR\n"
    "\n"
    "======================================================================\n"
    "ERROR: test_rejects_zero (tests.test_calc.DivTests.test_rejects_zero)\n"
    "Rejects a zero divisor.\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    '  File "/repo/tests/test_calc.py", line 13, in test_rejects_zero\n'
    "    div(1, 0)\n"
    "    ~~~^^^^^^\n"
    '  File "/repo/calc.py", line 2, in div\n'
    "    return a / b\n"
    "           ~~^~~\n"
    "ZeroDivisionError: division by zero\n"
    "\n"
    "----------------------------------------------------------------------\n"
    "Ran 2 tests in 0.001s\n"
    "\n"
    "FAILED (errors=1)\n"
)

_POST = (
    "test_divides (tests.test_calc.DivTests.test_divides) ... ok\n"
    "test_rejects_zero (tests.test_calc.DivTests.test_rejects_zero)\n"
    "Rejects a zero divisor. ... ok\n"
    "\n"
    "----------------------------------------------------------------------\n"
    "Ran 2 tests in 0.000s\n"
    "\n"
    "OK\n"
)


def test_unittest_fix_yields_an_oracle_and_grades_it(tmp_path: Path):
    cmd = ["python -m unittest -v"]
    pre, post = parse_logs(cmd, _PRE), parse_logs(cmd, _POST)
    f2p = sorted(
        name for name, st in pre.items() if st in ("FAILED", "ERROR") and post.get(name) == "PASSED"
    )
    p2p = sorted(name for name, st in pre.items() if st == "PASSED" and post.get(name) == "PASSED")
    assert f2p == ["tests.test_calc.DivTests.test_rejects_zero"]
    assert p2p == ["tests.test_calc.DivTests.test_divides"]

    # Run the verifier in isolation, with no installed repo2rlenv imports.
    standalone = tmp_path / "verifier.py"
    standalone.write_text(Path(verifier.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "out.log").write_text(_POST, encoding="utf-8")
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
            "python -m unittest -v",
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
    assert details["runner"] == "unittest"
    assert details["reward"] == 1.0
    assert details["resolved"] is True


# A real fix whose failing tests are subtests: `is_odd` is corrected, and one
# subtest value is a tuple, so its parameters contain nested parentheses.
_SUB_PRE = (
    "test_all_odd (tests.test_sub.SubTests.test_all_odd) ... \n"
    "  test_all_odd (tests.test_sub.SubTests.test_all_odd) (value=3) ... FAIL\n"
    "test_pairs (tests.test_sub.SubTests.test_pairs) ... \n"
    "  test_pairs (tests.test_sub.SubTests.test_pairs) (value=(3, 4)) ... FAIL\n"
    "\n"
    "======================================================================\n"
    "FAIL: test_all_odd (tests.test_sub.SubTests.test_all_odd) (value=3)\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    '  File "/repo/tests/test_sub.py", line 10, in test_all_odd\n'
    "    self.assertTrue(is_odd(value))\n"
    "    ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^\n"
    "AssertionError: False is not true\n"
    "\n"
    "======================================================================\n"
    "FAIL: test_pairs (tests.test_sub.SubTests.test_pairs) (value=(3, 4))\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    '  File "/repo/tests/test_sub.py", line 15, in test_pairs\n'
    "    self.assertTrue(is_odd(pair[0]))\n"
    "    ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^\n"
    "AssertionError: False is not true\n"
    "\n"
    "----------------------------------------------------------------------\n"
    "Ran 2 tests in 0.001s\n"
    "\n"
    "FAILED (failures=2)\n"
)

_SUB_POST = (
    "test_all_odd (tests.test_sub.SubTests.test_all_odd) ... ok\n"
    "test_pairs (tests.test_sub.SubTests.test_pairs) ... ok\n"
    "\n"
    "----------------------------------------------------------------------\n"
    "Ran 2 tests in 0.000s\n"
    "\n"
    "OK\n"
)


def test_subtest_failures_report_under_the_parent(parser):
    assert parser(_SUB_PRE) == {
        "tests.test_sub.SubTests.test_all_odd": "FAILED",
        "tests.test_sub.SubTests.test_pairs": "FAILED",
    }
    assert parser(_SUB_POST) == {
        "tests.test_sub.SubTests.test_all_odd": "PASSED",
        "tests.test_sub.SubTests.test_pairs": "PASSED",
    }


def test_subtests_can_transition_from_failing_to_passing():
    # Keeping the parameters would leave a FAIL_TO_PASS name that the repaired
    # run never prints, since it reports only `parent ... ok`.
    cmd = ["python -m unittest -v"]
    pre, post = parse_logs(cmd, _SUB_PRE), parse_logs(cmd, _SUB_POST)
    f2p = sorted(
        name for name, st in pre.items() if st in ("FAILED", "ERROR") and post.get(name) == "PASSED"
    )
    assert f2p == [
        "tests.test_sub.SubTests.test_all_odd",
        "tests.test_sub.SubTests.test_pairs",
    ]


@pytest.mark.parametrize(
    "params",
    ["(i=2)", "(value=(1, 2))", "(value={'a': 1})", "(msg='a (b) c')", "(a=1) (b=2)"],
)
def test_subtest_parameters_are_matched_but_not_kept(parser, params):
    log = f"  test_x (pkg.mod.Case.test_x) {params} ... FAIL\n"
    assert parser(log) == {"pkg.mod.Case.test_x": "FAILED"}


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("ok", "FAIL", "FAILED"),
        ("FAIL", "ok", "FAILED"),
        ("FAIL", "ERROR", "ERROR"),
        ("skipped 'x'", "ok", "PASSED"),
        ("ok", "skipped 'x'", "PASSED"),
    ],
)
def test_worst_status_wins_when_a_test_reports_twice(parser, first, second, expected):
    log = (
        f"  test_x (pkg.mod.Case.test_x) (i=1) ... {first}\n"
        f"  test_x (pkg.mod.Case.test_x) (i=2) ... {second}\n"
    )
    assert parser(log) == {"pkg.mod.Case.test_x": expected}
