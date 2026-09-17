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

from repo2rlenv.log_parsers.pytest_parser import TestStatus

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
        out[name] = status
    return out
