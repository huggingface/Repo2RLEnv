"""Pytest output parser — what the validation harness depends on."""

from __future__ import annotations

from repo2rlenv.log_parsers import parse_pytest


_PYTEST_SAMPLE = """\
============================= test session starts ==============================
platform darwin -- Python 3.12.1, pytest-8.3.0
collected 4 items

tests/test_a.py::test_one PASSED                                         [ 25%]
tests/test_a.py::test_two FAILED                                         [ 50%]
tests/test_b.py::test_three SKIPPED                                      [ 75%]
tests/test_b.py::test_four PASSED                                        [100%]

=================================== FAILURES ===================================
______________________________ test_two ______________________________
    >       assert add(2, 3) == 6
E       AssertionError
=========================== short test summary info ============================
PASSED tests/test_a.py::test_one
FAILED tests/test_a.py::test_two - AssertionError: assert 5 == 6
SKIPPED tests/test_b.py::test_three
PASSED tests/test_b.py::test_four
=================== 2 passed, 1 failed, 1 skipped in 0.04s ====================
"""


def test_parses_canonical_pytest_summary():
    status = parse_pytest(_PYTEST_SAMPLE)
    assert status["tests/test_a.py::test_one"] == "PASSED"
    assert status["tests/test_a.py::test_two"] == "FAILED"
    assert status["tests/test_b.py::test_three"] == "SKIPPED"
    assert status["tests/test_b.py::test_four"] == "PASSED"


def test_failed_line_strips_message_after_dash():
    """`FAILED tests/foo.py::test_x - AssertionError: ...` should yield just the test name."""
    status = parse_pytest("FAILED tests/foo.py::test_x - AssertionError: nope\n")
    assert "tests/foo.py::test_x" in status
    assert status["tests/foo.py::test_x"] == "FAILED"


def test_empty_log_returns_empty_dict():
    assert parse_pytest("") == {}
    assert parse_pytest(None) == {}  # type: ignore[arg-type]


def test_parametrized_test_names_preserved():
    """pytest -v parametrized tests use bracket notation that mustn't be split."""
    log = "PASSED tests/test_p.py::test_x[case-1]\n"
    status = parse_pytest(log)
    assert status == {"tests/test_p.py::test_x[case-1]": "PASSED"}


def test_skipped_with_count_prefix_skips_bracket_token():
    """Lines like 'SKIPPED [1] tests/foo.py:42' should still record the file token."""
    log = "SKIPPED [1] tests/foo.py:42\n"
    status = parse_pytest(log)
    # The third token is the actual file location
    assert "tests/foo.py:42" in status
    assert status["tests/foo.py:42"] == "SKIPPED"


def test_last_write_wins_for_same_test():
    log = "SKIPPED tests/foo.py::test_x\nPASSED tests/foo.py::test_x\n"
    status = parse_pytest(log)
    assert status["tests/foo.py::test_x"] == "PASSED"
