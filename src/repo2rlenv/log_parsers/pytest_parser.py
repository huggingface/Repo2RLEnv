"""Pytest output parser.

Pytest emits per-test status lines in TWO formats, depending on flags:

  1. Verbose progress (pytest -v):
       tests/test_foo.py::test_a PASSED                                 [25%]
       tests/test_foo.py::test_b FAILED                                 [50%]

  2. Short summary (always, at end of run):
       PASSED tests/test_foo.py::test_a
       FAILED tests/test_foo.py::test_b - AssertionError: ...

The earlier version of this parser only handled format (2), so with `-v`
output (the progress lines that come first) it returned an empty map.
We now match both — progress lines AND summary lines — and last-write-wins
so a test that progress-printed PASSED then re-appeared in the summary
still ends up as PASSED.

Adapted from SWE-bench's harness/log_parsers/python.py:parse_log_pytest.
Independent implementation; Apache-2.0.
"""

from __future__ import annotations

import re
from typing import Literal

TestStatus = Literal["PASSED", "FAILED", "SKIPPED", "ERROR"]

_STATUSES = ("PASSED", "FAILED", "SKIPPED", "ERROR")

# Verbose progress format: `tests/foo.py::test_x PASSED  [12%]` or `... FAILED`
# Test names contain alphanumerics, _, /, ., ::, [, ], -, +
# We require AT LEAST ONE non-whitespace token followed by whitespace + STATUS.
_VERBOSE_RE = re.compile(
    r"^(?P<name>\S+)\s+(?P<status>PASSED|FAILED|SKIPPED|ERROR)\b"
)


def parse_pytest(log: str) -> dict[str, TestStatus]:
    """Return {test_name -> status} parsed from pytest output (any verbosity).

    Notes:
      - Last-write-wins. A test that appears in both progress AND summary
        ends up as whichever appeared last (typically summary, which is fine).
      - SKIPPED lines like `SKIPPED [1] tests/foo.py:42` have the [N] count
        prefix stripped so the test name is the third token, not '[1]'.
      - Lines like `FAILED tests/foo.py::test_x - AssertionError: ...`
        get the dash chunk stripped to keep the test name clean.
      - Returns an empty dict for empty/malformed input. Caller decides what
        to do; usually treat as "test suite didn't run, env issue".
    """
    out: dict[str, TestStatus] = {}
    if not log:
        return out
    for raw in log.split("\n"):
        line = raw.strip()
        if not line:
            continue

        # --- format (2): summary lines (STATUS first) ---
        # Has to be checked BEFORE the verbose regex because a summary line
        # like "PASSED tests/foo.py::test_a" would also match the verbose
        # pattern (with name="PASSED" — wrong).
        leading_status: TestStatus | None = None
        for st in _STATUSES:
            if line.startswith(st + " ") or line == st:
                leading_status = st  # type: ignore[assignment]
                break
        if leading_status is not None:
            work = line
            if leading_status == "FAILED" and " - " in work:
                work = work.split(" - ", 1)[0]
            tokens = work.split()
            if len(tokens) < 2:
                continue
            test_name = tokens[1]
            # SKIPPED [N] file:line  → the [N] is a count, real name is tokens[2]
            if test_name.startswith("[") and test_name.endswith("]"):
                if len(tokens) < 3:
                    continue
                test_name = tokens[2]
            out[test_name] = leading_status
            continue

        # --- format (1): verbose progress (NAME first, STATUS after) ---
        m = _VERBOSE_RE.match(line)
        if m:
            name = m.group("name")
            # Heuristic: a real test name contains '::' (pytest node id) OR
            # is a path ending in .py. Avoids matching random lines like
            # "Some line PASSED something" where "Some" isn't a test.
            if "::" in name or name.endswith(".py"):
                out[name] = m.group("status")  # type: ignore[assignment]
            continue
    return out
