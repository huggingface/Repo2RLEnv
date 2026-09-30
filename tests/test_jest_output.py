"""Jest file headers must be read by both parsers, with or without a project name."""

from __future__ import annotations

import pytest

from repo2rlenv.log_parsers.jest_parser import parse_jest
from repo2rlenv.pipelines import _pr_runtime_verifier as verifier


@pytest.fixture(params=[parse_jest, verifier.parse_jest], ids=["canonical", "standalone"])
def parser(request):
    return request.param


# jest `projects` with a `displayName` each: getResultHeader prints the name
# between the label and the path. Both files have an `A > works` test.
_PROJECTS = (
    "PASS spec test/a.spec.js\n"
    "  A\n"
    "    ✓ works (1 ms)\n"
    "\n"
    "FAIL unit src/a.test.js (5.123 s)\n"
    "  A\n"
    "    ✓ works\n"
    "    ✕ fails (2 ms)\n"
)

# The same headers with color on: the label and name are padded, so after
# stripping escape codes there are extra spaces around them.
_PROJECTS_COLOR = (
    "\x1b[0m\x1b[7m\x1b[1m\x1b[32m PASS \x1b[39m\x1b[22m\x1b[27m\x1b[0m "
    "\x1b[0m\x1b[7m\x1b[37m spec \x1b[39m\x1b[27m\x1b[0m "
    "\x1b[2mtest/\x1b[22m\x1b[1ma.spec.js\x1b[22m\n"
    "  A\n"
    "    \x1b[32m✓\x1b[39m \x1b[2mworks (1 ms)\x1b[22m\n"
    "\n"
    "\x1b[0m\x1b[7m\x1b[1m\x1b[31m FAIL \x1b[39m\x1b[22m\x1b[27m\x1b[0m "
    "\x1b[0m\x1b[7m\x1b[37m unit \x1b[39m\x1b[27m\x1b[0m "
    "\x1b[2msrc/\x1b[22m\x1b[1ma.test.js\x1b[22m\n"
    "  A\n"
    "    \x1b[32m✓\x1b[39m \x1b[2mworks\x1b[22m\n"
    "    \x1b[31m✕\x1b[39m \x1b[2mfails (2 ms)\x1b[22m\n"
)


@pytest.mark.parametrize("log", [_PROJECTS, _PROJECTS_COLOR], ids=["plain", "color"])
def test_project_display_name_keeps_the_file(parser, log):
    assert parser(log) == {
        "test/a.spec.js > A > works": "PASSED",
        "src/a.test.js > A > works": "PASSED",
        "src/a.test.js > A > fails": "FAILED",
    }


# jest's default testMatch also picks up `.mts` / `.cts` files.
def test_mts_and_cts_files_get_their_own_header(parser):
    log = (
        "PASS src/a.test.js\n"
        "  A\n"
        "    ✓ works\n"
        "PASS src/b.test.mts\n"
        "  B\n"
        "    ✓ works\n"
        "FAIL src/c.test.cts\n"
        "  C\n"
        "    ✕ works\n"
    )
    assert parser(log) == {
        "src/a.test.js > A > works": "PASSED",
        "src/b.test.mts > B > works": "PASSED",
        "src/c.test.cts > C > works": "FAILED",
    }
