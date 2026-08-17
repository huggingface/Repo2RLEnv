"""Shared verifier-script + diff helpers (`pipelines/_eval_script.py`).

Ported from the former test_pipeline_mutation_bugs.py when mutation_bugs was
removed; these helpers now back code_instruct + equivalence_tests.
"""

from __future__ import annotations

from repo2rlenv.pipelines._eval_script import (
    authed_clone_url,
    build_binary_eval_script,
    env_prelude_from_test_cmds,
    make_unified_diff,
    normalize_test_cmds_for_runtime,
)

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


# ---------------------------------------------------------------------------
# normalize_test_cmds_for_runtime (moved from pr_runtime.py — pr_runtime
# re-exports it; see test_pipeline_pr_runtime.py for the re-export contract)
# ---------------------------------------------------------------------------


def test_blank_test_cmds_dropped():
    """Degenerate bootstrap-recorded entries that reduce to '' after the
    pipe/redirect strippers must be dropped, not emitted as empty segments
    (an empty segment in `" && ".join(...)` is a bash syntax error)."""
    assert normalize_test_cmds_for_runtime(
        ["| head -50", "2>&1", "  ", ". /workspace/.venv/bin/activate && pytest -v"]
    ) == [". /workspace/.venv/bin/activate && pytest -v"]


# ---------------------------------------------------------------------------
# env_prelude_from_test_cmds (new)
# ---------------------------------------------------------------------------


def test_env_prelude_from_test_cmds():
    # Leading venv-activation fragment is extracted with no trailing `&&`.
    assert (
        env_prelude_from_test_cmds([". /workspace/.venv/bin/activate && pytest -v"])
        == ". /workspace/.venv/bin/activate"
    )
    # No env-setup fragment present at all → literal "true" (a no-op to source).
    assert env_prelude_from_test_cmds(["pytest -v"]) == "true"
    # A fragment containing a single quote must round-trip unmangled — the
    # prelude is *sourced*, never interpolated into a shell string.
    cmds = ["export PYTEST_ADDOPTS='-p no:randomly' && pytest -v"]
    assert env_prelude_from_test_cmds(cmds) == "export PYTEST_ADDOPTS='-p no:randomly'"


# ---------------------------------------------------------------------------
# authed_clone_url (new)
# ---------------------------------------------------------------------------


def test_authed_clone_url_handles_gitlab():
    assert (
        authed_clone_url("https://github.com/o/r.git")
        == "https://x-access-token:${GITHUB_TOKEN}@github.com/o/r.git"
    )
    assert (
        authed_clone_url("https://gitlab.com/o/r.git")
        == "https://oauth2:${GITHUB_TOKEN}@gitlab.com/o/r.git"
    )
    assert (
        authed_clone_url("https://gitlab.com/o/r.git", arg_name="GIT_TOKEN")
        == "https://oauth2:${GIT_TOKEN}@gitlab.com/o/r.git"
    )
