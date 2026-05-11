"""Pytest output parser.

Pytest's default output is line-oriented:

    PASSED tests/test_foo.py::test_a
    FAILED tests/test_foo.py::test_b - AssertionError: ...
    SKIPPED [1] tests/test_bar.py:42: reason

We match the leading status keyword and pick the second token as the test
name. Parametrized tests (e.g. `test_x[case-1]`) come through as a single
token because pytest doesn't split on spaces inside `[...]`.

Adapted from SWE-bench's harness/log_parsers/python.py:parse_log_pytest.
Independent implementation; Apache-2.0.
"""

from __future__ import annotations

from typing import Literal

TestStatus = Literal["PASSED", "FAILED", "SKIPPED", "ERROR"]

# Order matters: longer prefixes first so "PASSED" isn't matched as part of
# something else. All four are the canonical pytest single-letter -v statuses.
_STATUS_PREFIXES: tuple[TestStatus, ...] = ("PASSED", "FAILED", "SKIPPED", "ERROR")


def parse_pytest(log: str) -> dict[str, TestStatus]:
    """Return {test_name -> status} parsed from pytest -v output.

    Notes:
      - Last-write-wins if the same test name appears twice (pytest
        sometimes prints SKIPPED early then PASSED on retry).
      - Lines like 'FAILED tests/foo.py::test_x - AssertionError: ...' have
        the dash chunk stripped so the test name is exactly the second token.
      - Returns an empty dict for empty/malformed input. Caller decides what
        to do — usually treat as "test suite didn't run, env issue".
    """
    out: dict[str, TestStatus] = {}
    if not log:
        return out
    for raw in log.split("\n"):
        line = raw.strip()
        if not line:
            continue
        for status in _STATUS_PREFIXES:
            if line.startswith(status):
                # 'FAILED tests/foo.py::test_x - AssertionError' → drop after ' - '
                if status == "FAILED" and " - " in line:
                    line = line.split(" - ", 1)[0]
                tokens = line.split()
                if len(tokens) < 2:
                    break
                test_name = tokens[1]
                # SKIPPED lines sometimes look like 'SKIPPED [1] tests/foo.py:42' —
                # the [N] count isn't a test name. Skip those rather than recording
                # nonsense entries.
                if test_name.startswith("[") and test_name.endswith("]"):
                    if len(tokens) < 3:
                        break
                    test_name = tokens[2]
                out[test_name] = status
                break
    return out
