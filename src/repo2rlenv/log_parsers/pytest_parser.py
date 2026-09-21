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
# Anchor the status to pytest's trailing reason/progress fields, so spaces and
# status words inside a parameter ID remain part of the name.
# console_output_style uses percentages, counts, or format_node_duration times.
_PROGRESS = (
    r"\[\s*\d+%\]|\[\s*\d+\s*/\s*\d+\s*\]"
    r"|\d+(?:\.\d+)?(?:us|ms|s)|\d+m \d+s|\d+h \d+m"
)
_VERBOSE_RE = re.compile(
    r"^(?P<name>.+?(?:\[.*?\])?)\s+(?P<status>PASSED|FAILED|SKIPPED|ERROR)"
    rf"(?:\s+\(.*\))?(?:\s+(?:{_PROGRESS}))?$"
)
# Consume a bracketed parameter suffix before looking for the diagnostic dash.
# Lazy matching keeps brackets in the diagnostic from becoming part of the ID.
_SUMMARY_NAME_RE = re.compile(r"^(?P<name>.+?(?:\[.*?\])?)(?: - .*)?$")
# pytest-xdist labels every result with the worker that ran it and moves the
# progress counter in front of the status, leaving the summary form behind it:
#   `[gw1] [ 20%] PASSED tests/foo.py::test_x`  (also `[1/5]`, or neither)
_XDIST_PREFIX_RE = re.compile(rf"^\[gw\d+\]\s+(?:(?:{_PROGRESS})\s+)?")


def parse_pytest(log: str) -> dict[str, TestStatus]:
    """Return {test_name -> status} parsed from pytest output (any verbosity).

    Notes:
      - Last-write-wins. A test that appears in both progress AND summary
        ends up as whichever appeared last (typically summary, which is fine).
      - SKIPPED lines like `SKIPPED [1] tests/foo.py:42` record the file
        location rather than the count prefix or skip reason.
      - Lines like `FAILED tests/foo.py::test_x - AssertionError: ...`
        get the dash chunk stripped to keep the test name clean.
      - pytest-xdist's `[gw1] [ 20%] PASSED ...` worker label is stripped,
        so distributed runs record every test, not just the failures that
        pytest repeats in its summary.
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
        # Drop the xdist worker label so both formats below match as usual.
        line = _XDIST_PREFIX_RE.sub("", line, count=1)

        # --- format (2): summary lines (STATUS first) ---
        # Handle the leading status separately from the full node ID.
        leading_status: TestStatus | None = None
        for st in _STATUSES:
            if line.startswith(st + " ") or line == st:
                leading_status = st  # type: ignore[assignment]
                break
        if leading_status is not None:
            work = line[len(leading_status) :].strip()
            if leading_status == "SKIPPED" and re.match(r"^\[\d+\](?:\s|$)", work):
                # Folded skips report a file location, not a parametrized ID.
                tokens = work.split(maxsplit=2)
                if len(tokens) < 2:
                    continue
                out[tokens[1]] = leading_status
            elif m := _SUMMARY_NAME_RE.match(work):
                out[m.group("name")] = leading_status
            continue

        # --- format (1): verbose progress (NAME first, STATUS after) ---
        # Reject assertion diffs and other non-test output before the regex:
        # bracket-heavy lines can otherwise cause expensive backtracking.
        head = line.split(None, 1)[0]
        if "::" not in head and not head.endswith(".py"):
            continue
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
