"""Jest output parser.

Jest's default reporter prints per-test status lines using Unicode glyphs:

    PASS  src/foo.test.ts
      Foo
        ✓ returns 200 (4 ms)
        ✕ returns 500 (1 ms)

    FAIL  src/bar.test.ts
      Bar > nested describe
        ✓ does the thing (12 ms)
        ○ skipped: tagged xit

    Tests:       2 failed, 4 passed, 1 skipped, 7 total

We reconstruct the qualified test name from the surrounding `describe` /
file scope by tracking indentation:

    file      = "src/foo.test.ts"
    describe  = ["Foo"]
    test_name = "src/foo.test.ts > Foo > returns 200"

This is enough to compute FAIL_TO_PASS / PASS_TO_PASS because the same name
appears in both pre- and post-fix runs of `validate_pr`.

Glyph legend:
  ✓ / √ / "PASS"  → PASSED
  ✕ / × / "FAIL"  → FAILED
  ○ / "skipped"   → SKIPPED

Mocha (the other common JS runner) uses the same ✓/✕ glyphs, so this parser
covers most mocha output too.

Vitest (3+) is detected by its ` RUN  vX.Y.Z` / ` Test Files` markers and read
from `--reporter=verbose`, which prints one fully qualified line per test:

     ✓ src/foo.test.ts > Foo > returns 200 1ms
     × |unit| src/foo.test.ts > Foo > returns 500 3ms

The name is kept as printed, minus the duration and retry/heap/note suffixes.
Vitest's default reporter collapses fully passing files to one summary line,
so its per-test lines are ignored: a test would vanish from the log as soon as
a patch fixed the rest of its file. Vitest also colors output whenever TERM is
unset, as in a non-TTY `docker exec`, so escape codes are stripped first.

Released under Apache-2.0.
"""

from __future__ import annotations

import re

from repo2rlenv.log_parsers.pytest_parser import TestStatus

# File header: `PASS src/foo.test.ts (123 ms)` or `FAIL src/foo.test.ts`.
# Captures the file path so we can prefix it onto test names.
# With color, the label is padded (` FAIL `), leaving one leading space.
_JEST_FILE_RE = re.compile(
    r"^ ?(?:PASS|FAIL)\s+(?P<path>\S+\.(?:ts|tsx|js|jsx|mjs|cjs))\b",
)

# Per-test glyph line. Indented arbitrarily; the glyph is the discriminator.
# Captures the visible name (everything after the glyph + space, minus the
# trailing ` (NN ms)` timing).
_JEST_TEST_RE = re.compile(
    r"^(?P<indent>\s*)(?P<glyph>✓|√|✕|×|✗|○|◯)\s+(?P<name>.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$",
)

_GLYPH_STATUS: dict[str, TestStatus] = {
    "✓": "PASSED",
    "√": "PASSED",
    "✕": "FAILED",
    "×": "FAILED",
    "✗": "FAILED",
    "○": "SKIPPED",
    "◯": "SKIPPED",
}

# Terminal escape codes (colors), stripped before any matching.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# ` RUN  v5.0.1 /repo` starts a vitest run; ` Test Files  1 passed (1)` ends it.
_VITEST_MARKER_RE = re.compile(r"^\s*(?:RUN\s+v\d+\.\d+|Test Files\s+\d)", re.MULTILINE)

# A `--reporter=verbose` test line. The optional project label is `|unit| `
# without color and ` unit  ` with it. Suffixes follow vitest's
# getTestCaseSuffix: duration, retries, repeats, heap usage, skip note.
_VITEST_TEST_RE = re.compile(
    r"^\s*(?P<glyph>[✓×↓□]) (?:\|(?P<project>[^|]+)\| | (?P<label>\S+)  )?"
    r"(?P<name>\S+ > .+?)(?P<duration> \d{1,9}ms)?"
    r"(?: \(retry x\d{1,9}\))?(?: \(repeat x\d{1,9}\))?(?: \d{1,9} MB heap used)?(?: \[[^\[\]]*\])?$"
)

_VITEST_STATUS: dict[str, TestStatus] = {
    "✓": "PASSED",
    "×": "FAILED",
    "↓": "SKIPPED",
    "□": "SKIPPED",
}


def _parse_vitest(log: str) -> dict[str, TestStatus]:
    out: dict[str, TestStatus] = {}
    for raw in log.split("\n"):
        m = _VITEST_TEST_RE.match(raw.rstrip())
        if not m:
            continue
        glyph = m.group("glyph")
        name = m.group("name")
        # Only finished tests print a duration; keep a skipped title's own "5ms".
        if glyph in "↓□" and m.group("duration"):
            name += m.group("duration")
        project = m.group("project") or m.group("label")
        out[f"|{project}| {name}" if project else name] = _VITEST_STATUS[glyph]
    return out


def parse_jest(log: str) -> dict[str, TestStatus]:
    """Return {test_name -> status} parsed from Jest / Mocha / Vitest output.

    Test names are qualified with their file path and describe chain so
    parametrized tests (`> case-1`) and same-name tests across files stay
    distinct. Lines that don't match a file header or a test glyph are
    skipped — they're describe-block headers, expectation errors, summary
    output, etc.
    """
    out: dict[str, TestStatus] = {}
    if not log:
        return out
    log = _ANSI_RE.sub("", log)
    if _VITEST_MARKER_RE.search(log):
        return _parse_vitest(log)

    current_file: str | None = None
    # describe stack indexed by indent depth (in characters). On a new test
    # line at indent N, the prefix is every entry with indent < N.
    describe_stack: list[tuple[int, str]] = []
    # Track the lowest indent we've seen for tests; describe headers are
    # anything above that level. We rebuild this opportunistically.
    last_test_indent: int | None = None

    for raw in log.split("\n"):
        line = raw.rstrip()
        if not line:
            continue

        # File header — resets describe stack
        m = _JEST_FILE_RE.match(line)
        if m:
            current_file = m.group("path")
            describe_stack = []
            last_test_indent = None
            continue

        # Test line
        m = _JEST_TEST_RE.match(line)
        if m:
            indent = len(m.group("indent"))
            status = _GLYPH_STATUS[m.group("glyph")]
            name = m.group("name").strip()
            # Trim any "skipped: " / "todo: " prefix that Jest prepends for ○
            name = re.sub(r"^(?:skipped|todo):\s*", "", name)
            # Anything in describe_stack with indent strictly less than the
            # test's indent is an enclosing describe block
            describes = [entry for ind, entry in describe_stack if ind < indent]
            parts = ([current_file] if current_file else []) + describes + [name]
            test_name = " > ".join(parts)
            out[test_name] = status
            last_test_indent = indent
            continue

        # Possibly a describe-block header line. Heuristic: non-empty line
        # whose indent is less than the most recent test's indent, AND it
        # doesn't start with a known runner-output token (PASS/FAIL/Tests/
        # error markers). If we don't have a test indent yet, any line that
        # follows a file header is a candidate describe.
        stripped = line.lstrip()
        if not stripped:
            continue
        if stripped.startswith(("Tests:", "Test Suites:", "Snapshots:", "Time:", "Ran all")):
            continue
        if stripped.startswith(("●", "→", "✗:")):
            # error or failure summary block
            continue
        indent_here = len(line) - len(stripped)
        # Only treat as describe if it sits above the current test indent
        # (or there's no test seen yet but a file is in scope)
        if current_file and (last_test_indent is None or indent_here < last_test_indent):
            # Drop any describes at >= this indent (we descended out of them)
            describe_stack = [(i, d) for i, d in describe_stack if i < indent_here]
            describe_stack.append((indent_here, stripped))

    return out
