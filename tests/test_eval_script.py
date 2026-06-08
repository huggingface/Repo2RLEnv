"""Shared verifier-script + diff helpers (`pipelines/_eval_script.py`).

Ported from the former test_pipeline_mutation_bugs.py when mutation_bugs was
removed; these helpers now back code_instruct + equivalence_tests.
"""

from __future__ import annotations

from repo2rlenv.pipelines._eval_script import build_binary_eval_script, make_unified_diff

# ---------------------------------------------------------------------------
# make_unified_diff
# ---------------------------------------------------------------------------


def test_make_unified_diff_empty_on_no_change():
    assert make_unified_diff("x = 1\n", "x = 1\n", "a.py") == ""


def test_make_unified_diff_has_git_header():
    diff = make_unified_diff("x = 1\n", "x = 2\n", "src/foo.py")
    assert diff.startswith("diff --git a/src/foo.py b/src/foo.py\n")
    assert "--- a/src/foo.py" in diff
    assert "+++ b/src/foo.py" in diff
    assert "-x = 1" in diff
    assert "+x = 2" in diff


def test_make_unified_diff_ends_with_newline():
    diff = make_unified_diff("x = 1\n", "x = 2\n", "a.py")
    assert diff.endswith("\n")


def test_make_unified_diff_round_trip():
    """The diff we emit should reverse cleanly (forward + gold)."""
    old = "def f(x):\n    return x + 1\n"
    new = "def f(x):\n    return x - 1\n"
    fwd = make_unified_diff(old, new, "f.py")
    rev = make_unified_diff(new, old, "f.py")
    assert "+    return x - 1" in fwd
    assert "+    return x + 1" in rev


# ---------------------------------------------------------------------------
# build_binary_eval_script
# ---------------------------------------------------------------------------


def test_eval_script_writes_reward():
    script = build_binary_eval_script(["pytest tests/test_foo.py -v"], language="python")
    assert "START_TEST_OUTPUT" in script
    assert "END_TEST_OUTPUT" in script
    assert "/logs/verifier/reward.txt" in script
    assert "pytest tests/test_foo.py -v" in script


def test_eval_script_includes_path_prelude_for_go():
    script = build_binary_eval_script(["go test ./..."], language="go")
    assert "/usr/local/go/bin" in script


def test_eval_script_no_prelude_for_python():
    script = build_binary_eval_script(["pytest"], language="python")
    assert "/usr/local/go/bin" not in script
