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

New tasks run `cargo test --message-format=json`: Cargo's JSON messages
(https://doc.rust-lang.org/cargo/reference/external-tools.html#json-messages)
tie each test binary's `Running` header to its package and target, so results
are keyed `<package>/<kind>/<target>::<test>` (doctests `<package>/doc/<lib>`),
and a target named by a compiler error in a failed build gets a unit-level
ERROR. Logs without those messages keep the legacy, unqualified keys.

Released under Apache-2.0.
"""

from __future__ import annotations

import json
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


# `     Running unittests src/lib.rs (target/debug/deps/mycrate-9311986e2699f121)`
_RUNNING_RE = re.compile(r"^\s*Running\s+\S.*\((?P<executable>[^()]+)\)\s*$")
# `   Doc-tests mycrate`
_DOCTESTS_RE = re.compile(r"^\s*Doc-tests\s+(?P<lib>\S+)\s*$")
_LIB_KINDS = frozenset({"lib", "rlib", "dylib", "cdylib", "staticlib", "proc-macro"})


def _file_name(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _cargo_package(package_id: str) -> str:
    """The package name in a Cargo package ID.

    Cargo 1.77+ writes `path+file:///w/crates/foo#foo@0.1.0`, or `...#0.1.0` when
    the name matches the URL's last path segment; older Cargo `foo 0.1.0 (path+...)`.
    """
    if " " in package_id:
        return package_id.split(" ", 1)[0]
    url, _, fragment = package_id.partition("#")
    name, at, _version = fragment.rpartition("@")
    if at:
        return name
    return url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def _cargo_unit(message: dict) -> str | None:
    """`<package>/<kind>/<target>` for a Cargo JSON message, or None if malformed."""
    package, target = message.get("package_id"), message.get("target")
    if not isinstance(package, str) or not isinstance(target, dict):
        return None
    name, kinds = target.get("name"), target.get("kind")
    if not isinstance(name, str) or not name or not isinstance(kinds, list) or not kinds:
        return None
    kind = "lib" if _LIB_KINDS.intersection(map(str, kinds)) else str(kinds[0])
    unit = f"{_cargo_package(package)}/{kind}/{name}"
    return None if "::" in unit or any(c.isspace() for c in unit) else unit


def parse_cargo_test(log: str) -> dict[str, TestStatus]:
    """Return {test_name -> status} parsed from `cargo test` output.

    With Cargo's JSON messages, tests are keyed `<unit>::<test>` and a target
    with explicit build-failure evidence gets a unit-level ERROR (see the
    module docstring). Lines that don't match the `test NAME ... STATUS` shape
    are ignored: build output, failure detail blocks and the final summary.
    Keep this implementation in sync with the standalone task verifier.
    """
    out: dict[str, TestStatus] = {}
    if not log:
        return out
    legacy: dict[str, TestStatus] = {}
    structured = False
    executables: dict[str, str] = {}
    libraries: dict[str, str] = {}
    build_errors: set[str] = set()
    build_failed = False
    unit: str | None = None
    for raw in log.split("\n"):
        if raw.startswith("{"):
            try:
                message = json.loads(raw)
            except (ValueError, RecursionError):
                message = None
            reason = message.get("reason") if isinstance(message, dict) else None
            if reason in ("compiler-artifact", "compiler-message", "build-finished"):
                structured = True
                found = _cargo_unit(message)
                if reason == "compiler-artifact" and found:
                    executable = message.get("executable")
                    # Cargo reports every artifact before any test runs; the
                    # first mapping wins, so JSON printed by a test can't move
                    # a later binary's results to another target.
                    if isinstance(executable, str) and executable:
                        executables.setdefault(_file_name(executable), found)
                    if found.split("/")[1] == "lib":
                        libraries.setdefault(message["target"]["name"], found.split("/")[0])
                elif reason == "compiler-message" and found:
                    detail = message.get("message")
                    if isinstance(detail, dict) and detail.get("level") == "error":
                        build_errors.add(found)
                elif reason == "build-finished" and message.get("success") is False:
                    build_failed = True
                continue
        m = _RUNNING_RE.match(raw)
        if m:
            unit = executables.get(_file_name(m.group("executable").strip()))
            continue
        m = _DOCTESTS_RE.match(raw)
        if m:
            lib = m.group("lib")
            unit = f"{libraries[lib]}/doc/{lib}" if lib in libraries else None
            continue
        m = _CARGO_TEST_RE.match(raw)
        if not m:
            continue
        name = _TEST_MODE_RE.sub("", m.group("name")).rstrip()
        status = _STATUS_MAP[m.group("status")]
        doctest = _DOCTEST_RE.match(name)
        if doctest:
            item = doctest.group("item")
            name = f"{doctest.group('file')} - {item}" if item else doctest.group("file")
        _record_worst(legacy, name, status)
        if unit is not None:
            _record_worst(out, f"{unit}::{name}", status)
    if not structured:
        return legacy
    if build_failed:
        for failed in build_errors:
            out[failed] = "ERROR"
            # A repeated command that passed once then failed to build must not
            # retain a passing result from the earlier run.
            for name in out:
                if name.startswith(failed + "::"):
                    _record_worst(out, name, "ERROR")
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
