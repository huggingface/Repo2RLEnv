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

Released under Apache-2.0.
"""

from __future__ import annotations

import re

from repo2rlenv.log_parsers.pytest_parser import TestStatus

# `test <path::to::test> ... ok` / `... FAILED` / `... ignored`
# The leading anchor avoids matching `--- test result:` summary lines.
_CARGO_TEST_RE = re.compile(
    r"^test\s+(?P<name>\S+)\s+\.\.\.\s+(?P<status>ok|FAILED|ignored)\b",
)

_STATUS_MAP: dict[str, TestStatus] = {
    "ok": "PASSED",
    "FAILED": "FAILED",
    "ignored": "SKIPPED",
}


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
        if m:
            out[m.group("name")] = _STATUS_MAP[m.group("status")]
    return out
