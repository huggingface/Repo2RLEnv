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
We now match both — progress lines AND summary lines. A test reported more
than once keeps its worst status (ERROR > FAILED > PASSED > SKIPPED), so a
test that progress-printed PASSED then re-appeared in the summary still ends
up as PASSED, while a fake `PASSED` line printed by code under test can't
overwrite pytest's real `FAILED`.

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
_PROGRESS_RE = re.compile(_PROGRESS)
_WHITESPACE_RE = re.compile(r"\s")


def _verbose_match(line: str) -> tuple[str, TestStatus] | None:
    """(name, status) of a verbose result line, exactly as
    `^(.+?(?:\\[.*?\\])?)\\s+(PASSED|FAILED|SKIPPED|ERROR)(?:\\s+\\(.*\\))?(?:\\s+(?:PROGRESS))?$`
    reads it, in linear time (that regex backtracked quadratically).

    The regex takes the shortest name, trying at each length first a bracket
    group closed by the first workable "]", then the bare name. A name end E
    works when the rest of the line, from E, is whitespace, a status word and
    an optional "(reason)" and/or progress field up to the end of the line.
    """
    size = len(line)
    if size < 2:
        return None
    space = [_WHITESPACE_RE.match(char) is not None for char in line]
    # next_solid[i]: the first non-whitespace position at or after i.
    next_solid = [size] * (size + 1)
    for index in range(size - 1, -1, -1):
        next_solid[index] = next_solid[index + 1] if space[index] else index
    # Positions where a progress field starts after whitespace and runs to the end.
    progress = {
        start
        for start in range(1, size)
        if space[start - 1] and _PROGRESS_RE.fullmatch(line, start) is not None
    }
    # The last ")" that can close a "(reason)": the line's last character, or
    # the ")" right before the whitespace that precedes a progress field.
    last_close = size - 1 if line[-1] == ")" else -1
    for start in progress:
        run = start - 1
        while run > 0 and space[run - 1]:
            run -= 1
        if run > 0 and line[run - 1] == ")":
            last_close = max(last_close, run - 1)

    def tail_ok(position: int) -> bool:
        if position == size:
            return True
        solid = next_solid[position]
        if solid in (position, size):
            return False
        return solid in progress or (line[solid] == "(" and last_close > solid)

    def rest(end: int) -> TestStatus | None:
        if end >= size or not space[end]:
            return None
        solid = next_solid[end]
        for status in _STATUSES:
            if line.startswith(status, solid) and tail_ok(solid + len(status)):
                return status
        return None

    # good_close[i]: the first "]" at or after i whose next position works as a name end.
    good_close = [-1] * (size + 1)
    for index in range(size - 1, -1, -1):
        good_close[index] = (
            index if line[index] == "]" and rest(index + 1) else good_close[index + 1]
        )
    for length in range(1, size):
        if line[length] == "[" and good_close[length + 1] >= 0:
            end = good_close[length + 1] + 1
            return line[:end], rest(end)  # type: ignore[return-value]
        status = rest(length)
        if status is not None:
            return line[:length], status
    return None


# pytest-xdist labels every result with the worker that ran it and moves the
# progress counter in front of the status, leaving the summary form behind it:
#   `[gw1] [ 20%] PASSED tests/foo.py::test_x`  (also `[1/5]`, or neither)
_XDIST_PREFIX_RE = re.compile(rf"^\[gw\d+\]\s+(?:(?:{_PROGRESS})\s+)?")
_RANK: dict[TestStatus, int] = {"SKIPPED": 0, "PASSED": 1, "FAILED": 2, "ERROR": 3}


def _summary_name(work: str) -> str:
    """The node ID of a summary line, as `^(.+?(?:\\[.*?\\])?)(?: - .*)?$` reads
    it, in linear time (that regex backtracked quadratically on bracket-heavy
    lines): the shortest prefix that ends where the line does or where " - "
    starts, optionally extended by a bracket group to the first "]" that is
    itself followed by the end or " - "."""
    size = len(work)

    def cut(position: int) -> bool:
        return position == size or work.startswith(" - ", position)

    # closing[i]: the first "]" at or after i that is followed by a cut.
    closing = [-1] * (size + 1)
    for index in range(size - 1, -1, -1):
        closing[index] = index if work[index] == "]" and cut(index + 1) else closing[index + 1]
    for length in range(1, size + 1):
        if length < size and work[length] == "[" and closing[length] >= 0:
            return work[: closing[length] + 1]
        if cut(length):
            return work[:length]
    return work


def _record_worst(out: dict[str, TestStatus], name: str, status: TestStatus) -> None:
    if name in out and _RANK[out[name]] >= _RANK[status]:
        return
    out[name] = status


def parse_pytest(log: str) -> dict[str, TestStatus]:
    """Return {test_name -> status} parsed from pytest output (any verbosity).

    Notes:
      - Worst-status-wins. A test that appears in both progress AND summary
        keeps the worse of the two (ERROR > FAILED > PASSED > SKIPPED), so
        output printed by the code under test can't upgrade a failure.
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
            # unittest's footer is `FAILED (failures=1, errors=1)`, which is a
            # count, not a node ID. Without this it lands as a test named
            # `(errors=1)` whenever a unittest log reaches this parser.
            if work.startswith("(") and work.endswith(")"):
                continue
            if leading_status == "SKIPPED" and re.match(r"^\[\d+\](?:\s|$)", work):
                # Folded skips report a file location, not a parametrized ID.
                tokens = work.split(maxsplit=2)
                if len(tokens) < 2:
                    continue
                _record_worst(out, tokens[1], leading_status)
            elif work:
                _record_worst(out, _summary_name(work), leading_status)
            continue

        # --- format (1): verbose progress (NAME first, STATUS after) ---
        # Reject assertion diffs and other non-test output before matching.
        head = line.split(None, 1)[0]
        if "::" not in head and not head.endswith(".py"):
            continue
        match = _verbose_match(line)
        if match:
            name, status = match
            # Heuristic: a real test name contains '::' (pytest node id) OR
            # is a path ending in .py. Avoids matching random lines like
            # "Some line PASSED something" where "Some" isn't a test.
            if "::" in name or name.endswith(".py"):
                _record_worst(out, name, status)
            continue
    return out
