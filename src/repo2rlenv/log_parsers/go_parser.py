"""Go test JSON events, with a legacy verbose-text fallback.

New tasks use ``go test -json`` and key tests as ``package::test``. Build
failures are scoped to the package reported by Go, not every absent test.
See https://pkg.go.dev/cmd/test2json for the event contract.

Legacy verbose output uses marker lines:

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
the second token (`TestName/subname` for subtests). A later passing line
cannot overwrite a failure, including failures in repeated `-count=N` runs.

Released under Apache-2.0.
"""

from __future__ import annotations

import json
import re

from repo2rlenv.log_parsers.pytest_parser import TestStatus, _record_worst

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
    """Parse Go JSON events as ``package::test``, or legacy verbose text.

    A package with explicit build-failure evidence gets a package-level ERROR.
    Validation can attribute post-fix tests to that package without promoting
    unrelated tests. Output events are never parsed as test results. Keep this
    implementation in sync with the standalone task verifier.
    """
    out: dict[str, TestStatus] = {}
    legacy: dict[str, TestStatus] = {}
    structured = False
    build_errors: set[str] = set()
    package_failures: set[str] = set()
    for raw in log.split("\n"):
        try:
            event = json.loads(raw) if raw.startswith("{") else None
        except (ValueError, RecursionError):
            event = None
        if not isinstance(event, dict):
            m = _GO_TEST_RE.match(raw)
            if m:
                _record_worst(legacy, m.group("name"), _STATUS_MAP[m.group("status")])
            continue
        package, action, test = event.get("Package"), event.get("Action"), event.get("Test")
        if not isinstance(package, str) or not package or "::" in package:
            continue
        if not isinstance(action, str):
            continue
        structured = True
        if isinstance(test, str) and test:
            status = _STATUS_MAP.get(action.upper())
            if status:
                _record_worst(out, f"{package}::{test}", status)
        elif not test:
            if action == "fail":
                package_failures.add(package)
                if isinstance(event.get("FailedBuild"), str) and event["FailedBuild"]:
                    build_errors.add(package)
            # Go before FailedBuild was introduced reports this exact package
            # summary in an output event, followed by the package's fail event.
            elif action == "output" and event.get("Output") == f"FAIL\t{package} [build failed]\n":
                build_errors.add(package)
    if not structured:
        return legacy
    for package in build_errors & package_failures:
        out[package] = "ERROR"
        # A repeated command that passed once then failed to build must not
        # retain a passing result from the earlier run.
        for name in out:
            if name.startswith(package + "::"):
                _record_worst(out, name, "ERROR")
    return out
