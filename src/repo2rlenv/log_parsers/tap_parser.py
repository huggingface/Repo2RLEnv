"""TAP (Test Anything Protocol) output parser.

QUnit's CLI, tape, `node --test --test-reporter=tap`, `mocha --reporter tap`,
`vitest --reporter=tap` and node-tap all print TAP, one test point per line:

    TAP version 13
    ok 1 add and subtract > add short reverse args
    not ok 2 add and subtract > broken one
      ---
      message: wrong
      severity: failed
      ...
    ok 3 add and subtract > skipped one # SKIP
    not ok 4 add and subtract > todo one # TODO
    1..4
    # pass 1

Only a test point at column 0 counts. The YAML failure blocks, nested subtests,
`# diagnostics`, the `1..N` plan and `Bail out!` are indented or not test points,
so they are skipped. The number and an optional `- ` before the description are
dropped; the description is the test name, so it must be unique per test (QUnit
prints the module path in it, `module > test`).

Statuses follow the runner's own verdict, as for unittest: `not ok # TODO` is an
expected failure and counts as PASSED, `# SKIP` is SKIPPED, any other `not ok`
is FAILED. TAP has no separate error status.

A test reported more than once keeps its worst status, so a line printed by the
code under test (`ok 7 name`) cannot overwrite the runner's real `not ok`.

Whether a log is TAP is decided by the command (`qunit`, `--reporter tap`) or by
its `TAP version N` header. Go prints `ok  \tpkg\t0.1s` per package, which this
parser would misread, so it never runs on a log with neither signal.

Released under Apache-2.0.
"""

from __future__ import annotations

import re

from repo2rlenv.log_parsers.pytest_parser import TestStatus, _record_worst

# `ok`, `not ok`, an optional test number, an optional `- `, then the description.
_POINT_RE = re.compile(r"^(?P<not>not )?ok(?:\s+(?P<num>\d+))?(?:\s+-)?(?:\s+(?P<name>.*))?$")
# `# SKIP` / `# TODO` after the description (case-insensitive, optional reason).
_DIRECTIVE_RE = re.compile(r"\s+#\s*(?P<word>skip|todo)\b", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# The header that marks a log as TAP when the command doesn't say so.
TAP_HEADER_RE = re.compile(r"^TAP version \d+", re.MULTILINE)


def parse_tap(log: str) -> dict[str, TestStatus]:
    """Return {test_name -> status} parsed from TAP output."""
    out: dict[str, TestStatus] = {}
    if not log:
        return out
    for raw in _ANSI_RE.sub("", log).split("\n"):
        line = raw.rstrip()
        if not line.startswith(("ok", "not ok")):
            continue
        m = _POINT_RE.match(line)
        if not m:
            continue
        name = (m.group("name") or "").strip()
        directive = _DIRECTIVE_RE.search(name)
        word = directive.group("word").lower() if directive else ""
        if directive:
            name = name[: directive.start()].rstrip()
        if not name:
            name = f"test {m.group('num')}" if m.group("num") else ""
        if not name:
            continue
        if word == "skip":
            status: TestStatus = "SKIPPED"
        elif m.group("not") and word != "todo":
            status = "FAILED"
        else:
            status = "PASSED"
        _record_worst(out, name, status)
    return out
