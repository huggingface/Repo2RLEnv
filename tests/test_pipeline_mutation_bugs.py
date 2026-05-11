"""mutation_bugs — helpers, builders, and contract conformance.

The operator catalog is tested in test_mutation_operators.py. Here we
cover the pipeline's pure-Python pieces:

  - _make_unified_diff (round-trip via git apply)
  - _is_excluded (glob matching)
  - _slice_test_output (marker extraction)
  - _target_pytest_for_tests (test-file targeting)
  - build_mutation_environment_dockerfile (Dockerfile shape)
  - build_mutation_eval_script (test.sh shape)
  - Pipeline contract (requires_bootstrap, missing-bootstrap rejection)
"""

from __future__ import annotations

import pytest

from repo2rlenv.pipelines.mutation_bugs import (
    MutationBugsPipeline,
    _is_excluded,
    _make_unified_diff,
    _slice_test_output,
    _target_pytest_for_tests,
    build_mutation_environment_dockerfile,
    build_mutation_eval_script,
)
from repo2rlenv.spec.options import MutationBugsOptions

# ---------------------------------------------------------------------------
# _make_unified_diff
# ---------------------------------------------------------------------------


def test_make_unified_diff_empty_on_no_change():
    assert _make_unified_diff("x = 1\n", "x = 1\n", "a.py") == ""


def test_make_unified_diff_has_git_header():
    diff = _make_unified_diff("x = 1\n", "x = 2\n", "src/foo.py")
    assert diff.startswith("diff --git a/src/foo.py b/src/foo.py\n")
    assert "--- a/src/foo.py" in diff
    assert "+++ b/src/foo.py" in diff
    assert "-x = 1" in diff
    assert "+x = 2" in diff


def test_make_unified_diff_ends_with_newline():
    diff = _make_unified_diff("x = 1\n", "x = 2\n", "a.py")
    assert diff.endswith("\n")


def test_make_unified_diff_round_trip_via_difflib():
    """The diff we emit should reverse cleanly (forward + gold)."""
    old = "def f(x):\n    return x + 1\n"
    new = "def f(x):\n    return x - 1\n"
    fwd = _make_unified_diff(old, new, "f.py")
    rev = _make_unified_diff(new, old, "f.py")
    assert "+    return x - 1" in fwd
    assert "+    return x + 1" in rev


# ---------------------------------------------------------------------------
# _is_excluded
# ---------------------------------------------------------------------------


def test_is_excluded_test_dir():
    assert _is_excluded("tests/test_foo.py", ["tests/**", "test_**"])


def test_is_excluded_passes_src_files():
    assert not _is_excluded("src/foo/bar.py", ["tests/**", "test_**", "docs/**"])


def test_is_excluded_init_py():
    assert _is_excluded("src/foo/__init__.py", ["**/__init__.py"])


# ---------------------------------------------------------------------------
# _slice_test_output
# ---------------------------------------------------------------------------


def test_slice_test_output_trims_to_markers():
    raw = (
        "some prelude\n"
        ": 'START_TEST_OUTPUT'\n"
        "test_foo PASSED\n"
        "test_bar FAILED\n"
        ": 'END_TEST_OUTPUT'\n"
        "trailing junk\n"
    )
    sliced = _slice_test_output(raw)
    assert "test_foo PASSED" in sliced
    assert "trailing junk" not in sliced


def test_slice_test_output_passes_through_when_no_markers():
    raw = "no markers here"
    assert _slice_test_output(raw) == raw


# ---------------------------------------------------------------------------
# _target_pytest_for_tests
# ---------------------------------------------------------------------------


def test_target_pytest_appends_file_paths():
    cmds = ["pytest"]
    broken = ["tests/test_foo.py::test_bar", "tests/test_foo.py::test_baz"]
    out = _target_pytest_for_tests(cmds, broken)
    assert out == ["pytest tests/test_foo.py"]  # de-duped, one path


def test_target_pytest_keeps_multiple_unique_paths():
    cmds = ["pytest -v"]
    broken = ["tests/test_a.py::test_1", "tests/test_b.py::test_2"]
    out = _target_pytest_for_tests(cmds, broken)
    assert "tests/test_a.py" in out[0]
    assert "tests/test_b.py" in out[0]


def test_target_pytest_skips_when_no_parseable_paths():
    cmds = ["pytest"]
    broken = ["weird_test_name_without_path"]
    assert _target_pytest_for_tests(cmds, broken) == cmds


def test_target_pytest_skips_when_path_already_present():
    cmds = ["pytest tests/test_foo.py"]
    broken = ["tests/test_foo.py::test_bar"]
    # Shouldn't double-append
    out = _target_pytest_for_tests(cmds, broken)
    assert out[0].count("tests/test_foo.py") == 1


# ---------------------------------------------------------------------------
# build_mutation_environment_dockerfile
# ---------------------------------------------------------------------------


def test_dockerfile_uses_base64():
    diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n"
    df = build_mutation_environment_dockerfile("local/img:abc", diff)
    assert "FROM local/img:abc" in df
    assert "base64 -d" in df
    assert "git apply" in df
    # Defensive git install present
    assert "apt-get install" in df


def test_dockerfile_includes_diff_content_encoded():
    """The base64 chunk should decode back to the original diff."""
    import base64

    diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n"
    df = build_mutation_environment_dockerfile("local/img:abc", diff)
    # Pull the encoded blob out of the Dockerfile
    import re

    m = re.search(r"echo (\S+) \| base64", df)
    assert m, "couldn't find base64 echo line"
    decoded = base64.b64decode(m.group(1)).decode()
    assert decoded == diff


# ---------------------------------------------------------------------------
# build_mutation_eval_script
# ---------------------------------------------------------------------------


def test_eval_script_writes_reward():
    script = build_mutation_eval_script(["pytest tests/test_foo.py -v"], language="python")
    assert "START_TEST_OUTPUT" in script
    assert "END_TEST_OUTPUT" in script
    assert "/logs/verifier/reward.txt" in script
    assert "pytest tests/test_foo.py -v" in script


def test_eval_script_includes_path_prelude_for_go():
    script = build_mutation_eval_script(["go test ./..."], language="go")
    assert "/usr/local/go/bin" in script


def test_eval_script_no_prelude_for_python():
    script = build_mutation_eval_script(["pytest"], language="python")
    # No PATH= line — python doesn't need it
    assert "/usr/local/go/bin" not in script


# ---------------------------------------------------------------------------
# Pipeline contract
# ---------------------------------------------------------------------------


def test_mutation_bugs_requires_bootstrap_attr():
    assert MutationBugsPipeline.requires_bootstrap is True


def test_mutation_bugs_rejects_missing_bootstrap():
    from repo2rlenv.spec.input import (
        GenerationInput,
        LLMSpec,
        OutputSpec,
        PipelineName,
        PipelineSpec,
        RepoSpec,
    )

    gen_input = GenerationInput(
        repo=RepoSpec(url="huggingface/trl"),
        pipeline=PipelineSpec(name=PipelineName.MUTATION_BUGS, options={}),
        llm=LLMSpec(provider="anthropic", model="claude-sonnet-4-6"),
        output=OutputSpec(destination="./out", org="x", dataset_name="y"),
    )
    with pytest.raises(RuntimeError, match="requires a BootstrapResult"):
        MutationBugsPipeline(gen_input, MutationBugsOptions(), bootstrap=None)


def test_mutation_bugs_options_defaults():
    """Defaults are usable as-is (no required fields)."""
    opts = MutationBugsOptions()
    assert opts.limit == 50
    assert opts.min_tests_broken == 1
    assert opts.operators is None  # ⇒ use every operator
