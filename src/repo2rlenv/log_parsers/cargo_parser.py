"""Cargo / `cargo test` output parser.

Rust's libtest format (used by both `cargo test` and `rustc --test`) emits:

    running 3 tests
    test tests::add_works ... ok
    test tests::overflow_panics ... FAILED
    test tests::skipped_for_now ... ignored

    failures:

    ---- tests::overflow_panics stdout ----
    thread 'tests::overflow_panics' panicked at 'attempt to add ...'

    failures:
        tests::overflow_panics

    test result: FAILED. 1 passed; 1 failed; 1 ignored; 0 measured

Each test status appears on a `test NAME ... STATUS` line. The summary block
below repeats failure names but they're already known from earlier lines;
last-write-wins keeps the canonical first occurrence (or its later override
on re-run with `cargo test -- --no-fail-fast`).

Doctests and integration tests use the same format, just printed multiple
times (once per binary). Test names include their module path
(`tests::add_works` or `my_crate::module::tests::test_name`).

libtest appends a test mode to some names, and doctest names contain spaces:

    test tests::divide_by_zero - should panic ... ok
    test src/lib.rs - add (line 26) ... ok
    test src/lib.rs - add (line 30) - compile fail ... ok

The mode is display-only, so `tests::divide_by_zero` is the identity. A
doctest's `(line N)` moves whenever a patch edits code above it, so doctests
are keyed by file and item (`src/lib.rs - add`) and the worst status among
an item's doctests wins. Otherwise a patch that shifts lines would lose the
doctest from FAIL_TO_PASS / PASS_TO_PASS at grading time.

Released under Apache-2.0.
"""

from __future__ import annotations

import re

from repo2rlenv.log_parsers.pytest_parser import TestStatus, _record_worst

# `test <path::to::test> ... ok` / `... FAILED` / `... ignored`
# The leading anchor avoids matching `--- test result:` summary lines. libtest
# writes exactly `test {name} ... {status}`; matching the literal ` ... ` keeps
# names with spaces intact without backtracking over long lines.
_CARGO_TEST_RE = re.compile(
    r"^test\s+(?P<name>\S.*?) \.\.\. (?P<status>ok|FAILED|ignored)\b",
)
# ` - should panic`, ` - compile fail` (doctest), ` - compile` (no_run doctest)
_TEST_MODE_RE = re.compile(r" - (?:should panic|compile fail|compile)$")
# `src/lib.rs - math::div (line 10)`; crate-level docs have no item.
_DOCTEST_RE = re.compile(r"^(?P<file>\S+) - (?:(?P<item>.+?) )?\(line \d+\)$")

_STATUS_MAP: dict[str, TestStatus] = {
    "ok": "PASSED",
    "FAILED": "FAILED",
    "ignored": "SKIPPED",
}

_DOCTEST_RANK: dict[TestStatus, int] = {"SKIPPED": 0, "PASSED": 1, "FAILED": 2}


def parse_cargo_test(log: str) -> dict[str, TestStatus]:
    """Return {test_name -> status} parsed from `cargo test` output.

    Lines that don't match the `test NAME ... STATUS` shape are ignored —
    they're either build output (`Compiling ...`), failure detail blocks,
    or the final summary (`test result: ok. 5 passed; 0 failed; ...`).
    """
    out: dict[str, TestStatus] = {}
    if not log:
        return out
    for raw in log.split("\n"):
        m = _CARGO_TEST_RE.match(raw)
        if not m:
            continue
        name = _TEST_MODE_RE.sub("", m.group("name")).rstrip()
        status = _STATUS_MAP[m.group("status")]
        doctest = _DOCTEST_RE.match(name)
        if doctest:
            item = doctest.group("item")
            name = f"{doctest.group('file')} - {item}" if item else doctest.group("file")
            if name in out and _DOCTEST_RANK[out[name]] > _DOCTEST_RANK[status]:
                continue
        _record_worst(out, name, status)
    return out


# cargo-nextest prints one status line per test instead of libtest's lines:
#
#         PASS [   0.013s] (1/2) my-crate tests::accepts_even
#         FAIL [   0.013s] (2/2) my-crate tests::rejects_odd
#   TRY 1 FAIL [   0.003s] my-crate::basic test_flaky
#   TRY 2 PASS [   0.002s] my-crate::basic test_flaky
#    FLAKY 2/3 [   0.002s] my-crate::basic test_flaky
#         SLOW [> 60.000s] my-crate tests::slow       <- still running, ignored
#         SKIP [         ] (---) my-crate tests::ignored
#
# The `(1/2)` counter and the `[1/3]` stress-run index are optional. The test is
# keyed by binary id + test name, since two binaries can hold the same test
# path. Retries print one line per attempt and the summary repeats final
# statuses, so the last line for a test wins. Captured libtest output under a
# failing test is ignored: it is indented and isn't a nextest status line.
_NEXTEST_RE = re.compile(
    r"^\s*(?P<status>[A-Z][A-Z0-9 +/-]*?) \[\s*(?:(?P<running>>)?\s*\d+(?:\.\d+)?s)?\s*\] "
    r"(?:\[\d+/\d+\] )?(?:\((?:\s*\d+/\d+|[-─]+)\) )?(?P<name>\S+ \S.*?)\s*$"
)
_NEXTEST_PASSED_RE = re.compile(
    r"PASS|LEAK|TIMEOUT-PASS|SLOW|SLOW \+ LEAK|SLOW\+TMPASS|FLAKY \d+/\d+"
    r"|TRY \d+ (?:PASS|LEAK|TMPASS)"
)
_NEXTEST_FAILED_RE = re.compile(
    r"FAIL|FAIL \+ LEAK|LEAK-FAIL|TIMEOUT|XFAIL|ABORT|ABORT SIG \d+|SIG[A-Z0-9]+|FLKY-FL \d+/\d+"
    r"|TRY \d+ (?!START$|SLOW$|TRMNTG$)(?:[A-Z0-9+]+|SIG \d+)"
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def parse_cargo_nextest(log: str) -> dict[str, TestStatus]:
    """Return {"binary-id test::name" -> status} parsed from `cargo nextest run` output."""
    out: dict[str, TestStatus] = {}
    if not log:
        return out
    for raw in log.split("\n"):
        m = _NEXTEST_RE.match(_ANSI_RE.sub("", raw))
        if not m or m.group("running"):
            continue
        status = m.group("status")
        if status == "SKIP":
            out[m.group("name")] = "SKIPPED"
        elif _NEXTEST_PASSED_RE.fullmatch(status):
            out[m.group("name")] = "PASSED"
        elif _NEXTEST_FAILED_RE.fullmatch(status):
            out[m.group("name")] = "FAILED"
    return out
