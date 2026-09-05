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

# Verbose progress format: `tests/foo.py::test_x PASSED  [12%]` or `... FAILED`.
# Match the status from the right so spaces inside parametrized node IDs survive.
_VERBOSE_RE = re.compile(
    r"^(?P<name>.+)\s+(?P<status>PASSED|FAILED|SKIPPED|ERROR)(?:\s+\[\s*\d+%\])?$"
)
_COUNT_PREFIX_RE = re.compile(r"^\[\d+\]\s+(?P<name>.+)$")


def _strip_summary_diagnostic(name: str) -> str:
    """Strip pytest's ` - diagnostic` suffix outside parametrization brackets."""
    depth = 0
    for i, char in enumerate(name):
        if char == "[":
            depth += 1
        elif char == "]" and depth:
            depth -= 1
        elif depth == 0 and name.startswith(" - ", i):
            return name[:i]
    return name


def parse_pytest(log: str) -> dict[str, TestStatus]:
    """Return {test_name -> status} parsed from pytest output (any verbosity).

    Notes:
      - Last-write-wins. A test that appears in both progress AND summary
        ends up as whichever appeared last (typically summary, which is fine).
      - SKIPPED lines like `SKIPPED [1] tests/foo.py:42` have the [N] count
        prefix stripped.
      - Summary diagnostics such as ` - AssertionError: ...` are stripped
        without treating ` - ` inside a parametrized node ID as a boundary.
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
        # like "PASSED tests/foo.py::test_a" would otherwise look name-first.
        leading_status: TestStatus | None = None
        for st in _STATUSES:
            if line.startswith(st + " ") or line == st:
                leading_status = st  # type: ignore[assignment]
                break
        if leading_status is not None:
            test_name = line[len(leading_status) :].strip()
            if not test_name:
                continue
            count_match = _COUNT_PREFIX_RE.match(test_name)
            if count_match:
                test_name = count_match.group("name")
            if leading_status in ("FAILED", "ERROR"):
                test_name = _strip_summary_diagnostic(test_name)
            if test_name:
                out[test_name] = leading_status
            continue

        # --- format (1): verbose progress (NAME first, STATUS after) ---
        m = _VERBOSE_RE.match(line)
        if m:
            name = m.group("name")
            # Heuristic: a real test name contains '::' (pytest node id) OR
            # is a path ending in .py. Avoids matching random output.
            if "::" in name or name.endswith(".py"):
                out[name] = m.group("status")  # type: ignore[assignment]
            continue
    return out
