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
covers most mocha output too. Vitest's default reporter is jest-compatible
by design — also covered.

Released under Apache-2.0.
"""

from __future__ import annotations

import re

from repo2rlenv.log_parsers.pytest_parser import TestStatus

# File header: `PASS src/foo.test.ts (123 ms)` or `FAIL src/foo.test.ts`.
# Captures the file path so we can prefix it onto test names.
_JEST_FILE_RE = re.compile(
    r"^(?:PASS|FAIL)\s+(?P<path>\S+\.(?:ts|tsx|js|jsx|mjs|cjs))\b",
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
