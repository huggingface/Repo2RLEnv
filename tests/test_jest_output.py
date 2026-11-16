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


# Issue #255: jest prints a nested describe at the same indent as the direct
# tests before it. Suites below are expected in every fully qualified name.
_NESTED_AFTER_DIRECT = (
    "FAIL src/a.test.js\n"
    "  Parent\n"
    "    ✓ direct before (1 ms)\n"
    "    Nested\n"
    "      ✓ inner passes\n"
    "      ✕ inner fails (2 ms)\n"
    "    ✓ direct after\n"
)

_SIBLINGS_SAME_TITLE = (
    "FAIL src/b.test.js\n"
    "  Parent\n"
    "    ✓ direct\n"
    "    First\n"
    "      ✓ works\n"
    "    Second\n"
    "      ✕ works\n"
)

_JEST_NESTING_CASES = {
    "nested_after_direct": (
        _NESTED_AFTER_DIRECT,
        {
            "src/a.test.js > Parent > direct before": "PASSED",
            "src/a.test.js > Parent > Nested > inner passes": "PASSED",
            "src/a.test.js > Parent > Nested > inner fails": "FAILED",
            "src/a.test.js > Parent > direct after": "PASSED",
        },
    ),
    "siblings_same_title": (
        _SIBLINGS_SAME_TITLE,
        {
            "src/b.test.js > Parent > direct": "PASSED",
            "src/b.test.js > Parent > First > works": "PASSED",
            "src/b.test.js > Parent > Second > works": "FAILED",
        },
    ),
}


def test_nested_describe_after_a_direct_test_keeps_its_title(parser):
    out = parser(_NESTED_AFTER_DIRECT)
    assert out["src/a.test.js > Parent > Nested > inner passes"] == "PASSED"
    assert out["src/a.test.js > Parent > Nested > inner fails"] == "FAILED"


def test_direct_test_after_a_nested_describe_returns_to_the_parent(parser):
    out = parser(_NESTED_AFTER_DIRECT)
    assert out["src/a.test.js > Parent > direct after"] == "PASSED"
    assert "src/a.test.js > Parent > Nested > direct after" not in out


def test_sibling_describes_keep_same_titled_tests_apart(parser):
    assert parser(_SIBLINGS_SAME_TITLE) == _JEST_NESTING_CASES["siblings_same_title"][1]


@pytest.mark.parametrize(
    "first, second",
    [("✓", "✕"), ("✕", "✓")],
    ids=["pass-then-fail", "fail-then-pass"],
)
def test_repeated_nested_test_id_keeps_the_failure(parser, first, second):
    # Two same-titled describes after a direct test give one fully qualified
    # id twice; a failure in either one wins.
    log = (
        "FAIL src/c.test.js\n"
        "  Parent\n"
        "    ✓ direct\n"
        "    Same\n"
        f"      {first} works\n"
        "    Same\n"
        f"      {second} works\n"
    )
    assert parser(log) == {
        "src/c.test.js > Parent > direct": "PASSED",
        "src/c.test.js > Parent > Same > works": "FAILED",
    }


@pytest.mark.parametrize("case", sorted(_JEST_NESTING_CASES))
def test_nesting_parsers_agree(case):
    log, expected = _JEST_NESTING_CASES[case]
    assert parse_jest(log) == verifier.parse_jest(log) == expected
