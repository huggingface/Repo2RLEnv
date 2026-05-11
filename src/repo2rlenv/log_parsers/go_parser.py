"""Go test (`go test -v`) output parser.

Go's verbose test output uses unambiguous marker lines:

    === RUN   TestParseConfig
        ... test body output ...
    --- PASS: TestParseConfig (0.00s)
    === RUN   TestBrokenInput
    --- FAIL: TestBrokenInput (0.01s)
        parse_test.go:42: expected 5, got 6
    === RUN   TestSkippedForNow
    --- SKIP: TestSkippedForNow (0.00s)
        only_on_linux.go:11: skipping on darwin
    PASS
    ok      github.com/foo/bar    0.013s

For subtests, the indentation increases:

    --- PASS: TestParse (0.00s)
        --- PASS: TestParse/valid_input (0.00s)
        --- FAIL: TestParse/invalid_input (0.00s)

We treat the leading `---` line as the source of truth — its status keyword
is the test's outcome, regardless of indentation depth. The test name is
the second token (`TestName/subname` for subtests). Last-write-wins keeps
the lifecycle simple: re-runs of the same test (e.g. `-count=N`) end up
with whichever status came last.

Released under Apache-2.0.
"""

from __future__ import annotations

import re

from repo2rlenv.log_parsers.pytest_parser import TestStatus

# `---     PASS: TestName (0.00s)` with optional leading whitespace for subtests.
# Status keyword captured as PASS/FAIL/SKIP; we map those to the canonical
# PASSED/FAILED/SKIPPED via _STATUS_MAP.
_GO_TEST_RE = re.compile(
    r"^\s*---\s+(?P<status>PASS|FAIL|SKIP):\s+(?P<name>\S+)",
)

_STATUS_MAP: dict[str, TestStatus] = {
    "PASS": "PASSED",
    "FAIL": "FAILED",
    "SKIP": "SKIPPED",
}


def parse_go_test(log: str) -> dict[str, TestStatus]:
    """Return {test_name -> status} parsed from `go test -v` output.

    Test names include any subtest path (`TestX/subY/subZ`). Lines that
    don't match the `--- STATUS:` shape are ignored — they're either test
    body output (`fmt.Println` from inside the test) or package-level
    summary lines (`ok`, `FAIL <package>`, `PASS`).
    """
    out: dict[str, TestStatus] = {}
    if not log:
        return out
    for raw in log.split("\n"):
        m = _GO_TEST_RE.match(raw)
        if m:
            out[m.group("name")] = _STATUS_MAP[m.group("status")]
    return out
