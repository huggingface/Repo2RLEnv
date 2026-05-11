"""Unit tests for the pr_runtime pipeline.

We don't actually drive Docker here — that's covered by manual end-to-end
runs against real repos. These tests pin the pure-Python bits:

  * split_patch_and_test_patch — the SWE-bench heuristic
  * build_eval_script        — the test.sh that ships in each task
  * _files_in_patch / _word_count / lite_filter behavior
  * pipeline contract conformance (requires_bootstrap=True)
"""

from __future__ import annotations

import pytest

from repo2rlenv.pipelines.pr_runtime import (
    PRRuntimePipeline,
    _count_new_test_funcs,
    _files_in_patch,
    _path_is_test,
    build_environment_dockerfile,
    build_eval_script,
    normalize_test_cmds_for_runtime,
    split_patch_and_test_patch,
    targeted_test_cmds_for_pr,
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
    # True positives: files inside test directories
    assert _path_is_test("tests/test_foo.py")
    assert _path_is_test("src/e2e/something.py")
    assert _path_is_test("module/testing/util.py")
    # True positives: filename-level markers
    assert _path_is_test("src/foo_test.py")
    assert _path_is_test("pkg/util_test.go")
    assert _path_is_test("components/Foo.spec.ts")
    # True negatives: source files
    assert not _path_is_test("src/foo.py")
    assert not _path_is_test("")
    # True negatives: docs that happen to contain "test"/"testing" in path
    # (THE bug we shipped — `docs/testing.md` shouldn't be classified as a test)
    assert not _path_is_test("docs/testing.md")
    assert not _path_is_test("docs/test_strategy.md")
    assert not _path_is_test("examples/test_app.py")
    # True negatives: source files with "testing" in the path
    # (e.g. click's src/click/testing.py is the CliRunner module, not a test)
    assert not _path_is_test("src/click/testing.py")


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


def test_build_eval_script_preserves_test_exit_code():
    """P1 fix: cleanup `git checkout || true` must NOT mask pytest's failure.

    Harbor's verifier reads this script's exit code; if we always exit 0,
    every model patch looks like a pass regardless of test outcome.
    """
    script = build_eval_script(
        base_commit="a" * 40,
        test_patch="",
        test_cmds=["pytest -v"],
    )
    assert "TEST_EXIT_CODE=$?" in script
    assert "exit $TEST_EXIT_CODE" in script
    # The capture must come AFTER the test block and BEFORE the cleanup reset
    test_block_pos = script.find("pytest -v")
    capture_pos = script.find("TEST_EXIT_CODE=$?")
    final_exit_pos = script.find("exit $TEST_EXIT_CODE")
    assert test_block_pos < capture_pos < final_exit_pos


def test_build_eval_script_writes_reward_file():
    """Harbor verifier reads /logs/verifier/reward.txt — exit code alone isn't enough."""
    script = build_eval_script(
        base_commit="a" * 40,
        test_patch="",
        test_cmds=["pytest -v"],
    )
    assert "mkdir -p /logs/verifier" in script
    assert "/logs/verifier/reward.txt" in script
    # Should write 1.0 on pass, 0.0 on fail
    assert "1.0" in script and "0.0" in script


def test_build_eval_script_no_path_prelude_for_python():
    """Python is always on PATH in standard images; no prelude needed."""
    script = build_eval_script(
        base_commit="a" * 40,
        test_patch="",
        test_cmds=["pytest -v"],
        language="python",
    )
    assert "/usr/local/go/bin" not in script
    assert ".cargo/bin" not in script


def test_build_eval_script_path_prelude_for_go():
    """Go binaries typically install to /usr/local/go/bin — must be on PATH."""
    script = build_eval_script(
        base_commit="a" * 40,
        test_patch="",
        test_cmds=["go test -v ./..."],
        language="go",
    )
    assert "/usr/local/go/bin" in script
    # Export must precede the test block so `go test` resolves
    export_pos = script.find("export PATH")
    test_pos = script.find("go test")
    assert 0 <= export_pos < test_pos


def test_build_eval_script_path_prelude_for_rust():
    script = build_eval_script(
        base_commit="a" * 40,
        test_patch="",
        test_cmds=["cargo test"],
        language="rust",
    )
    assert ".cargo/bin" in script


# --- build_environment_dockerfile --------------------------------------------


def test_environment_dockerfile_resets_to_base_commit():
    """P1 fix: image must be at PR base_commit, not bootstrap HEAD.

    Without this, the model writes its patch against base_commit's line
    context but Harbor tries to apply it against bootstrap-HEAD's lines,
    so patches fail to apply.
    """
    dockerfile = build_environment_dockerfile(
        bootstrap_image="local/r2e/foo:abc",
        base_commit="d777956105fde08e01dd895dde2b86ccdf558d59",
    )
    assert "FROM local/r2e/foo:abc" in dockerfile
    assert "WORKDIR /workspace" in dockerfile
    # The reset must use the specific base_commit, not just any sha
    assert "git reset --hard d777956105fde08e01dd895dde2b86ccdf558d59" in dockerfile
    # And there should be a fetch in case the commit isn't in the shallow clone
    assert "git fetch" in dockerfile
    # Order matters: FROM → WORKDIR → fetch → reset
    pos_from = dockerfile.find("FROM ")
    pos_workdir = dockerfile.find("WORKDIR")
    pos_fetch = dockerfile.find("git fetch")
    pos_reset = dockerfile.find("git reset")
    assert pos_from < pos_workdir < pos_fetch < pos_reset


# --- _count_new_test_funcs ---------------------------------------------------


def test_count_new_test_funcs_python():
    """Counts +def test_* and +class .*Test in unified diffs."""
    diff = """\
+def test_one():
+    assert True
+
+    def test_method_inner(self):
+        pass
+
+class TestSomething:
+    def test_method(self):
+        pass
"""
    assert _count_new_test_funcs(diff) == 4


def test_count_new_test_funcs_ignores_modifications():
    """Lines without leading + (e.g. context lines, removals) don't count."""
    diff = """\
 def test_existing():
-def test_removed():
+def test_added():
     # context test_misleading docstring
+    # this is +just a comment with test_ word
"""
    # Only +def test_added() matches; everything else is removed/context/comment
    assert _count_new_test_funcs(diff) == 1


def test_count_new_test_funcs_handles_empty():
    assert _count_new_test_funcs("") == 0
    assert _count_new_test_funcs("   \n  ") == 0


def test_count_new_test_funcs_go_and_js():
    """Cross-language: Go's `func TestX(` and JS's `it(`/`test(`/`describe(`."""
    diff = """\
+func TestParseConfig(t *testing.T) {
+}
+it('returns 200', () => {});
+test('returns 200', () => {});
+describe('module', () => {});
"""
    # 1 Go + 1 it() + 1 test() + 1 describe() = 4
    assert _count_new_test_funcs(diff) == 4


# --- normalize_test_cmds_for_runtime -----------------------------------------


def test_normalize_strips_collect_only():
    """Bootstrap often records `--collect-only` for the fast smoke gate; we need
    actual test execution at validation time."""
    assert normalize_test_cmds_for_runtime(["pytest --collect-only"]) == ["pytest -v"]
    assert normalize_test_cmds_for_runtime(["pytest --collect-only tests/"]) == [
        "pytest tests/ -v"
    ] or normalize_test_cmds_for_runtime(["pytest --collect-only tests/"]) == ["pytest  tests/ -v"]


def test_normalize_strips_short_co():
    assert normalize_test_cmds_for_runtime(["pytest --co"]) == ["pytest -v"]


def test_normalize_preserves_existing_verbose():
    assert normalize_test_cmds_for_runtime(["pytest -v tests/"]) == ["pytest -v tests/"]
    assert normalize_test_cmds_for_runtime(["pytest -vv"]) == ["pytest -vv"]


def test_normalize_go_test_gets_v_flag():
    """`go test` without -v doesn't print --- PASS lines — parser needs them."""
    assert normalize_test_cmds_for_runtime(["go test ./..."]) == ["go test -v ./..."]
    # Already verbose ⇒ no change
    assert normalize_test_cmds_for_runtime(["go test -v ./..."]) == ["go test -v ./..."]


def test_normalize_cargo_strips_quiet():
    """`cargo test -q` swallows per-test lines; strip it to recover parseable output."""
    assert normalize_test_cmds_for_runtime(["cargo test -q"]) == ["cargo test"]
    assert normalize_test_cmds_for_runtime(["cargo test --quiet"]) == ["cargo test"]
    # Default `cargo test` is already parseable
    assert normalize_test_cmds_for_runtime(["cargo test"]) == ["cargo test"]


def test_normalize_jest_adds_verbose_and_strips_silent():
    assert normalize_test_cmds_for_runtime(["jest"]) == ["jest --verbose"]
    assert normalize_test_cmds_for_runtime(["jest --silent"]) == ["jest --verbose"]
    # Already verbose ⇒ keep
    assert normalize_test_cmds_for_runtime(["jest --verbose"]) == ["jest --verbose"]


def test_normalize_npm_test_unchanged_when_wrapper():
    """`npm test` is a wrapper — we can't safely add jest flags through it."""
    # Just verify it doesn't crash and doesn't corrupt the cmd
    out = normalize_test_cmds_for_runtime(["npm test"])
    assert out == ["npm test"]


# --- targeted_test_cmds_for_pr ----------------------------------------------


def test_targeted_appends_test_files_to_pytest():
    """pytest -v becomes pytest -v tests/foo.py when test_patch touches foo."""
    out = targeted_test_cmds_for_pr(["pytest -v"], ["tests/test_foo.py", "tests/test_bar.py"])
    assert out == ["pytest -v tests/test_foo.py tests/test_bar.py"]


def test_targeted_go_test_replaces_dot_dot_dot_with_packages():
    """`go test ./...` → `go test ./pkg/foo` when the test_patch only touches pkg/foo."""
    out = targeted_test_cmds_for_pr(
        ["go test -v ./..."],
        ["pkg/foo/foo_test.go", "pkg/foo/bar_test.go"],
    )
    # Both test files are in pkg/foo, so we get one package
    assert out == ["go test -v ./pkg/foo"]


def test_targeted_go_test_handles_multiple_packages():
    out = targeted_test_cmds_for_pr(
        ["go test -v ./..."],
        ["pkg/a/a_test.go", "pkg/b/b_test.go"],
    )
    assert "./pkg/a" in out[0]
    assert "./pkg/b" in out[0]


def test_targeted_jest_appends_test_files():
    """Jest accepts positional file paths like pytest."""
    out = targeted_test_cmds_for_pr(["jest --verbose"], ["src/foo.test.ts", "src/bar.test.js"])
    assert "src/foo.test.ts" in out[0]
    assert "src/bar.test.js" in out[0]


def test_targeted_cargo_no_op():
    """Rust's filter is name-substring not file path; we don't target."""
    out = targeted_test_cmds_for_pr(["cargo test"], ["src/lib.rs", "tests/integration_test.rs"])
    assert out == ["cargo test"]


def test_targeted_npm_test_passes_through():
    """npm test is a wrapper; we can't safely append positional args."""
    assert targeted_test_cmds_for_pr(["npm test"], ["src/foo.test.ts"]) == ["npm test"]


def test_targeted_skips_when_no_test_files():
    """Empty test_files → don't touch the command."""
    assert targeted_test_cmds_for_pr(["pytest -v"], []) == ["pytest -v"]


def test_targeted_skips_when_pytest_already_has_path():
    """If user already passed a path argument, don't double-up."""
    out = targeted_test_cmds_for_pr(["pytest -v tests/"], ["tests/test_foo.py"])
    assert out == ["pytest -v tests/"]


def test_targeted_filters_non_py_test_files():
    """Snapshot fixtures / YAML test data shouldn't end up as pytest args."""
    out = targeted_test_cmds_for_pr(
        ["pytest -v"],
        ["tests/snapshots/foo.json", "tests/test_real.py"],
    )
    assert out == ["pytest -v tests/test_real.py"]


# --- pipeline contract --------------------------------------------------------


def test_pr_runtime_requires_bootstrap_attr():
    assert PRRuntimePipeline.requires_bootstrap is True


def test_pr_runtime_rejects_missing_bootstrap():
    """Constructing without a BootstrapResult should fail loudly (not silently emit broken tasks)."""
    from repo2rlenv.spec.input import (
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
