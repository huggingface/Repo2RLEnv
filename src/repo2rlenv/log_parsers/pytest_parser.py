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
_PROGRESS_RE = re.compile(r"^\[\s*(?:\d+%|\d+/\d+)\]$")
_DURATION_RE = re.compile(
    r"^(?:\d+(?:\.\d+)?(?:ns|us|µs|ms|s)|(?:\d+h(?: \d+m)?|\d+m)(?: \d+(?:\.\d+)?s)?)$"
)
_FOLDED_SKIP_RE = re.compile(r"^\[\d+\]\s+(?P<location>\S+)(?:\s+.*)?$")
_FOLDED_SKIP_COUNT_RE = re.compile(r"^\[\d+\](?:\s|$)")


def _valid_verbose_tail(status: str, tail: str) -> bool:
    """Return whether text after a verbose pytest status is a known trailer."""
    tail = tail.strip()
    if not tail:
        return True

    if status == "SKIPPED" and tail.startswith("("):
        closing = tail.rfind(")")
        if closing < 0:
            return False
        tail = tail[closing + 1 :].strip()
        if not tail:
            return True

    return bool(_PROGRESS_RE.fullmatch(tail) or _DURATION_RE.fullmatch(tail))


def _parse_verbose_line(line: str) -> tuple[str, TestStatus] | None:
    """Parse a name-first pytest status line without greedy regex backtracking."""
    first_token = line.split(maxsplit=1)[0]
    if "::" not in first_token and not first_token.endswith(".py"):
        return None

    best: tuple[int, str, TestStatus] | None = None
    for status in _STATUSES:
        marker = f" {status}"
        pos = line.rfind(marker)
        while pos >= 0:
            if _valid_verbose_tail(status, line[pos + len(marker) :]):
                name = line[:pos].rstrip()
                if "::" in name or name.endswith(".py"):
                    typed_status: TestStatus = status  # type: ignore[assignment]
                    if best is None or pos > best[0]:
                        best = (pos, name, typed_status)
                    break
            pos = line.rfind(marker, 0, pos)

    if best is None:
        return None
    return best[1], best[2]


def _strip_summary_diagnostic(name: str) -> str:
    """Strip pytest's ` - diagnostic` suffix while preserving parameter text."""
    separator = " - "
    if separator not in name:
        return name

    # The first `::` belongs to the node ID. Diagnostics can themselves contain
    # `::` (for example `expected mod::func`), so searching from the last one
    # can move the parameter scan into the diagnostic and truncate the ID.
    search_from = name.find("::") + 2 if "::" in name else 0
    parameter_start = name.find("[", search_from)
    if parameter_start >= 0:
        pos = name.find(separator, parameter_start)
        while pos >= 0:
            if pos > 0 and name[pos - 1] == "]":
                return name[:pos]
            pos = name.find(separator, pos + len(separator))
        return name

    return name.split(separator, 1)[0]


def parse_pytest(log: str) -> dict[str, TestStatus]:
    """Return {test_name -> status} parsed from pytest output (any verbosity).

    Notes:
      - Last-write-wins. A test that appears in both progress AND summary
        ends up as whichever appeared last (typically summary, which is fine).
      - Folded skips like `SKIPPED [1] tests/foo.py:42: reason` record the
        file location, not the count prefix or skip reason.
      - Summary diagnostics are stripped without truncating ` - ` inside
        parametrized node IDs.
      - Verbose `%`, `count`, `times`, and skip-reason trailers are supported.
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
        leading_status: TestStatus | None = None
        for status in _STATUSES:
            if line.startswith(status + " ") or line == status:
                leading_status = status  # type: ignore[assignment]
                break

        if leading_status is not None:
            work = line[len(leading_status) :].strip()
            if not work:
                continue

            if leading_status == "SKIPPED":
                folded = _FOLDED_SKIP_RE.match(work)
                if folded:
                    out[folded.group("location")] = leading_status
                    continue
                if _FOLDED_SKIP_COUNT_RE.match(work):
                    continue

            test_name = _strip_summary_diagnostic(work)
            if test_name:
                out[test_name] = leading_status
            continue

        # --- format (1): verbose progress (NAME first, STATUS after) ---
        parsed = _parse_verbose_line(line)
        if parsed is not None:
            name, status = parsed
            out[name] = status

    return out
