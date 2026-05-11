"""code_instruct — diff builder, dockerfile shape, pipeline contract.

Sampler / parser / decontam are tested in test_oss_instruct_helpers.py.
Here we cover the pipeline-level pure-Python pieces and the contract.
"""

from __future__ import annotations

import pytest

from repo2rlenv.pipelines.code_instruct import (
    CodeInstructPipeline,
    _all_tests_passed,
    _make_two_file_diff,
    build_code_instruct_dockerfile,
)
from repo2rlenv.spec.options import CodeInstructOptions

# ---------------------------------------------------------------------------
# _make_two_file_diff
# ---------------------------------------------------------------------------


def test_two_file_diff_has_two_headers():
    diff = _make_two_file_diff(
        task_module_code="def add(x, y):\n    return x + y\n",
        test_code="from task_module import add\ndef test_add():\n    assert add(1, 2) == 3\n",
        test_filename="test_r2e_xyz.py",
    )
    assert diff.count("diff --git ") == 2
    assert "diff --git a/task_module.py b/task_module.py" in diff
    assert "diff --git a/test_r2e_xyz.py b/test_r2e_xyz.py" in diff


def test_two_file_diff_marks_new_files():
    diff = _make_two_file_diff(
        task_module_code="x = 1\n",
        test_code="from task_module import x\n",
        test_filename="test_r2e_xyz.py",
    )
    # Each file block opens with `new file mode` + `--- /dev/null`
    assert diff.count("new file mode") == 2
    assert diff.count("--- /dev/null") == 2


def test_two_file_diff_hunk_line_counts():
    diff = _make_two_file_diff(
        task_module_code="line1\nline2\nline3\n",
        test_code="from task_module import x\nx\n",
        test_filename="test_r2e.py",
    )
    # Hunk header for the 3-line task module
    assert "@@ -0,0 +1,3 @@" in diff
    # Hunk header for the 2-line test
    assert "@@ -0,0 +1,2 @@" in diff


def test_two_file_diff_handles_missing_trailing_newline():
    """If a file doesn't end with \\n, we emit the `\\ No newline at end of file` line."""
    diff = _make_two_file_diff(
        task_module_code="x = 1",  # no trailing newline
        test_code="from task_module import x\n",
        test_filename="test_r2e.py",
    )
    assert "\\ No newline at end of file" in diff


# ---------------------------------------------------------------------------
# build_code_instruct_dockerfile
# ---------------------------------------------------------------------------


def test_dockerfile_minimal_shape():
    df = build_code_instruct_dockerfile("local/img:abc")
    assert df.startswith("# Auto-generated") or "FROM local/img:abc" in df
    assert "FROM local/img:abc" in df
    # No patching at build time (unlike pr_runtime / mutation_bugs)
    assert "git apply" not in df
    # Defensive git install (so `git config` works inside container)
    assert "apt-get install" in df


# ---------------------------------------------------------------------------
# _all_tests_passed
# ---------------------------------------------------------------------------


def test_all_tests_passed_detects_passed_summary():
    log = "==== 3 passed in 0.12s ===="
    assert _all_tests_passed(log)


def test_all_tests_passed_rejects_failed():
    log = "==== 1 failed, 2 passed in 0.12s ===="
    assert not _all_tests_passed(log)


def test_all_tests_passed_rejects_no_collected():
    log = "ERROR: collected 0 items"
    assert not _all_tests_passed(log)


def test_all_tests_passed_rejects_collection_error():
    log = "ImportError: No module named 'task_module'\nERRORS\ncollected 0 items / 1 error\n"
    assert not _all_tests_passed(log)


# ---------------------------------------------------------------------------
# Pipeline contract
# ---------------------------------------------------------------------------


def test_code_instruct_requires_bootstrap_attr():
    assert CodeInstructPipeline.requires_bootstrap is True


def test_code_instruct_rejects_missing_bootstrap():
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
        pipeline=PipelineSpec(name=PipelineName.CODE_INSTRUCT, options={}),
        llm=LLMSpec(provider="anthropic", model="claude-sonnet-4-6"),
        output=OutputSpec(destination="./out", org="x", dataset_name="y"),
    )
    with pytest.raises(RuntimeError, match="requires a BootstrapResult"):
        CodeInstructPipeline(gen_input, CodeInstructOptions(), bootstrap=None)


def test_code_instruct_options_defaults():
    opts = CodeInstructOptions()
    assert opts.limit == 50
    assert opts.seed_min_loc == 30
    assert opts.seed_max_loc == 200
    assert opts.require_test_fails_without_oracle is True
    assert opts.require_test_passes_with_oracle is True
