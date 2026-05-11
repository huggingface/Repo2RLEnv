"""Unit tests for the pr_runtime pipeline.

We don't actually drive Docker here — that's covered by manual end-to-end
runs against real repos. These tests pin the pure-Python bits:

  * split_patch_and_test_patch — the SWE-bench heuristic
  * build_eval_script        — the test.sh that ships in each task
  * _files_in_patch / _word_count / lite_filter behavior
  * pipeline contract conformance (requires_bootstrap=True)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repo2rlenv.pipelines.pr_runtime import (
    PRRuntimePipeline,
    _files_in_patch,
    _path_is_test,
    build_eval_script,
    split_patch_and_test_patch,
)


# --- diff split ---------------------------------------------------------------


_SAMPLE_DIFF = """\
diff --git a/src/foo.py b/src/foo.py
index abc..def 100644
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,3 +1,4 @@
 def add(a, b):
-    return a - b
+    return a + b
+    # fix the off-by-one
diff --git a/tests/test_foo.py b/tests/test_foo.py
index 111..222 100644
--- a/tests/test_foo.py
+++ b/tests/test_foo.py
@@ -1,2 +1,3 @@
 def test_add():
-    assert add(2, 3) == -1  # buggy expectation
+    assert add(2, 3) == 5
+    assert add(0, 0) == 0
"""


def test_split_patch_and_test_patch_separates_by_path():
    patch, test_patch = split_patch_and_test_patch(_SAMPLE_DIFF)
    # Source hunk goes into patch
    assert "src/foo.py" in patch
    assert "tests/test_foo.py" not in patch
    # Test hunk goes into test_patch
    assert "tests/test_foo.py" in test_patch
    assert "src/foo.py" not in test_patch


def test_split_patch_handles_empty_diff():
    patch, test_patch = split_patch_and_test_patch("")
    assert patch == "" and test_patch == ""


def test_split_patch_handles_no_test_files():
    diff = """\
diff --git a/lib/util.py b/lib/util.py
@@ -1 +1 @@
-x = 1
+x = 2
"""
    patch, test_patch = split_patch_and_test_patch(diff)
    assert "lib/util.py" in patch
    assert test_patch == ""


def test_split_patch_renames_to_test_dir_count_as_test():
    """A file moving INTO tests/ should be in test_patch."""
    diff = """\
diff --git a/src/foo.py b/tests/test_foo.py
@@ -1 +1 @@
-x = 1
+x = 2
"""
    patch, test_patch = split_patch_and_test_patch(diff)
    # Either side being a test path classifies the hunk as a test patch
    assert "tests/test_foo.py" in test_patch
    assert patch == ""


def test_path_is_test_heuristic():
    assert _path_is_test("tests/test_foo.py")
    assert _path_is_test("src/e2e/something.py")
    assert _path_is_test("module/testing/util.py")
    assert _path_is_test("src/foo_test.py")  # "test" appears anywhere in the name
    assert not _path_is_test("src/foo.py")
    assert not _path_is_test("")


def test_files_in_patch_returns_unique_paths():
    paths = _files_in_patch(_SAMPLE_DIFF)
    assert paths == ["src/foo.py", "tests/test_foo.py"]


# --- eval script --------------------------------------------------------------


def test_build_eval_script_includes_markers_and_test_cmds():
    script = build_eval_script(
        base_commit="a" * 40,
        test_patch=_SAMPLE_DIFF,  # any non-empty diff works for the heredoc test
        test_cmds=["pytest -x tests/"],
    )
    assert "START_TEST_OUTPUT" in script
    assert "END_TEST_OUTPUT" in script
    assert "pytest -x tests/" in script
    # Reset comes before and after the test run (idempotency)
    assert script.count("git checkout") >= 2
    # Workspace + safe.directory wiring (matches SWE-bench)
    assert "/workspace" in script
    assert "safe.directory" in script


def test_build_eval_script_tolerates_no_test_files():
    """If test_patch is empty, the reset step shouldn't reference any files."""
    script = build_eval_script(
        base_commit="b" * 40,
        test_patch="",
        test_cmds=["pytest"],
    )
    assert "no test files to reset" in script
    assert "pytest" in script


def test_build_eval_script_joins_multiple_test_cmds_with_and():
    """Bootstrap-recorded test_cmds may have multiple steps; they share one shell."""
    script = build_eval_script(
        base_commit="c" * 40,
        test_patch="",
        test_cmds=["export PATH=/opt/bin:$PATH", "pytest --collect-only"],
    )
    assert "export PATH=/opt/bin:$PATH && pytest --collect-only" in script


# --- pipeline contract --------------------------------------------------------


def test_pr_runtime_requires_bootstrap_attr():
    assert PRRuntimePipeline.requires_bootstrap is True


def test_pr_runtime_rejects_missing_bootstrap():
    """Constructing without a BootstrapResult should fail loudly (not silently emit broken tasks)."""
    from repo2rlenv.spec.input import (
        AuthSpec,
        GenerationInput,
        LLMSpec,
        OutputSpec,
        PipelineName,
        PipelineSpec,
        RepoSpec,
    )
    from repo2rlenv.spec.options import PRRuntimeOptions

    gen_input = GenerationInput(
        repo=RepoSpec(url="huggingface/trl"),
        pipeline=PipelineSpec(name=PipelineName.PR_RUNTIME, options={}),
        llm=LLMSpec(provider="anthropic", model="claude-sonnet-4-6"),
        output=OutputSpec(destination="./out", org="x", dataset_name="y"),
    )
    options = PRRuntimeOptions()
    with pytest.raises(RuntimeError, match="requires a BootstrapResult"):
        PRRuntimePipeline(gen_input, options, bootstrap=None)
