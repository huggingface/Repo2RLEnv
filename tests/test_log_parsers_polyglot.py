"""Polyglot log parsers — Go / Cargo / Jest + the dispatcher.

Pytest parser has its own test file. These cover the runners we added for
v0.4: `go test -v`, `cargo test`, and Jest (which also handles Mocha and
Vitest by output compatibility).
"""

from __future__ import annotations

from repo2rlenv.log_parsers import (
    parse_cargo_test,
    parse_go_test,
    parse_jest,
    parse_logs,
)

# -------------------------- go test --------------------------------------------


_GO_SAMPLE = """\
=== RUN   TestParseConfig
--- PASS: TestParseConfig (0.00s)
=== RUN   TestBrokenInput
--- FAIL: TestBrokenInput (0.01s)
    parse_test.go:42: expected 5, got 6
=== RUN   TestSkippedForNow
--- SKIP: TestSkippedForNow (0.00s)
    only_on_linux.go:11: skipping on darwin
=== RUN   TestParseSubtests
=== RUN   TestParseSubtests/valid_input
--- PASS: TestParseSubtests/valid_input (0.00s)
=== RUN   TestParseSubtests/invalid_input
    parse_test.go:55: nope
--- FAIL: TestParseSubtests/invalid_input (0.00s)
--- FAIL: TestParseSubtests (0.00s)
PASS
ok      github.com/foo/bar    0.013s
"""


def test_go_parses_pass_fail_skip():
    status = parse_go_test(_GO_SAMPLE)
    assert status["TestParseConfig"] == "PASSED"
    assert status["TestBrokenInput"] == "FAILED"
    assert status["TestSkippedForNow"] == "SKIPPED"


def test_go_parses_subtests_with_slash_in_name():
    status = parse_go_test(_GO_SAMPLE)
    assert status["TestParseSubtests/valid_input"] == "PASSED"
    assert status["TestParseSubtests/invalid_input"] == "FAILED"
    # The parent test itself shows FAIL too
    assert status["TestParseSubtests"] == "FAILED"


def test_go_ignores_run_and_body_lines():
    """`=== RUN` and indented body output don't become test entries."""
    status = parse_go_test(_GO_SAMPLE)
    # Sanity: no key contains "RUN" or random log content
    assert all("RUN" not in k for k in status)
    assert "ok" not in status
    assert "PASS" not in status  # the standalone "PASS" / "ok" summary line


def test_go_empty_log():
    assert parse_go_test("") == {}


def test_go_indented_subtest_PASS_still_parses():
    """Indentation in front of --- PASS lines for subtests must not break the regex."""
    log = "    --- PASS: TestX/sub (0.00s)\n"
    assert parse_go_test(log) == {"TestX/sub": "PASSED"}


# -------------------------- cargo test -----------------------------------------


_CARGO_SAMPLE = """\
   Compiling foo v0.1.0
    Finished test [unoptimized + debuginfo] target(s) in 1.23s
     Running unittests src/lib.rs

running 4 tests
test tests::add_works ... ok
test tests::overflow_panics ... FAILED
test tests::skipped_for_now ... ignored
test tests::nested::sub_test ... ok

failures:

---- tests::overflow_panics stdout ----
thread 'tests::overflow_panics' panicked at 'attempt to add with overflow'

failures:
    tests::overflow_panics

test result: FAILED. 2 passed; 1 failed; 1 ignored; 0 measured; 0 filtered out
"""


def test_cargo_parses_ok_failed_ignored():
    status = parse_cargo_test(_CARGO_SAMPLE)
    assert status["tests::add_works"] == "PASSED"
    assert status["tests::overflow_panics"] == "FAILED"
    assert status["tests::skipped_for_now"] == "SKIPPED"
    assert status["tests::nested::sub_test"] == "PASSED"


def test_cargo_ignores_failure_block_repeats():
    """The `failures:` block lists names again; we shouldn't re-record them."""
    status = parse_cargo_test(_CARGO_SAMPLE)
    # Only the 4 actual test result lines should produce entries
    assert len(status) == 4


def test_cargo_ignores_summary():
    """`test result: FAILED. ...` is the suite summary, not a test result."""
    status = parse_cargo_test(_CARGO_SAMPLE)
    assert "result:" not in status
    assert all(not k.startswith("result") for k in status)


def test_cargo_empty_log():
    assert parse_cargo_test("") == {}


# -------------------------- jest -----------------------------------------------


_JEST_SAMPLE = """\
PASS  src/foo.test.ts
  Foo
    ✓ returns 200 (4 ms)
    ✕ returns 500 (1 ms)

FAIL  src/bar.test.ts
  Bar
    nested describe
      ✓ deep test (2 ms)
      ○ skipped: not yet
  Top level test
    ○ todo: write me

Test Suites: 1 failed, 1 passed, 2 total
Tests:       1 failed, 2 passed, 2 skipped, 5 total
"""


def test_jest_parses_pass_fail_skip_with_file_prefix():
    status = parse_jest(_JEST_SAMPLE)
    # Glyph determines status; name includes file + describe chain
    assert status["src/foo.test.ts > Foo > returns 200"] == "PASSED"
    assert status["src/foo.test.ts > Foo > returns 500"] == "FAILED"


def test_jest_handles_nested_describes():
    status = parse_jest(_JEST_SAMPLE)
    assert status["src/bar.test.ts > Bar > nested describe > deep test"] == "PASSED"
    assert status["src/bar.test.ts > Bar > nested describe > not yet"] == "SKIPPED"


def test_jest_strips_skipped_and_todo_prefixes():
    """`○ skipped: foo` and `○ todo: bar` should record just `foo` / `bar`."""
    status = parse_jest(_JEST_SAMPLE)
    # No name should retain the "skipped: " / "todo: " prefix
    assert all("skipped: " not in k and "todo: " not in k for k in status)


def test_jest_ignores_summary_lines():
    status = parse_jest(_JEST_SAMPLE)
    assert all("Test Suites" not in k for k in status)
    assert all(not k.startswith("Tests:") for k in status)


def test_jest_empty_log():
    assert parse_jest("") == {}


# -------------------------- dispatcher (parse_logs) ----------------------------


def test_parse_logs_dispatches_to_pytest():
    log = "tests/foo.py::test_a PASSED                  [50%]\n"
    out = parse_logs(["pytest -v"], log)
    assert out == {"tests/foo.py::test_a": "PASSED"}


def test_parse_logs_dispatches_to_go():
    log = "--- PASS: TestFoo (0.00s)\n"
    out = parse_logs(["go test -v ./..."], log)
    assert out == {"TestFoo": "PASSED"}


def test_parse_logs_dispatches_to_cargo():
    log = "test tests::foo ... ok\n"
    out = parse_logs(["cargo test"], log)
    assert out == {"tests::foo": "PASSED"}


def test_parse_logs_dispatches_to_jest():
    log = "PASS  src/foo.test.ts\n  ✓ ok (1 ms)\n"
    out = parse_logs(["jest --verbose"], log)
    assert "src/foo.test.ts > ok" in out
    assert out["src/foo.test.ts > ok"] == "PASSED"


def test_parse_logs_falls_back_to_language_hint():
    """When test_cmds is a wrapper script we can't read, use the language."""
    log = "--- PASS: TestThing (0.00s)\n"
    out = parse_logs(["./run-tests.sh"], log, language="go")
    assert out == {"TestThing": "PASSED"}


def test_parse_logs_unknown_runner_returns_empty():
    """If we can't identify the runner AND no language hint, return {}.

    Caller (validate_pr) treats empty as 'no F2P possible', which is the
    safe failure mode.
    """
    out = parse_logs(["./weird-runner.sh"], "some output")
    assert out == {}


def test_parse_logs_npm_test_maps_to_jest():
    """`npm test` is overwhelmingly jest in modern repos; dispatch accordingly."""
    log = "PASS  src/foo.test.ts\n  ✓ thing\n"
    out = parse_logs(["npm test --silent"], log)
    assert any("src/foo.test.ts" in k for k in out)
