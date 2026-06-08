"""code_instruct — diff builder, dockerfile shape, pipeline contract.

Sampler / parser / decontam are tested in test_oss_instruct_helpers.py.
Here we cover the pipeline-level pure-Python pieces and the contract.
"""

from __future__ import annotations

import pytest

from repo2rlenv.pipelines.code_instruct import (
    CodeInstructPipeline,
    _all_tests_passed,
    build_code_instruct_dockerfile,
    make_solution_diff,
)
from repo2rlenv.spec.options import CodeInstructOptions

# ---------------------------------------------------------------------------
# make_solution_diff — gold patch carries ONLY task_module.py (issue #54)
# ---------------------------------------------------------------------------


def test_solution_diff_has_single_header():
    diff = make_solution_diff(task_module_code="def add(x, y):\n    return x + y\n")
    assert diff.count("diff --git ") == 1
    assert "diff --git a/task_module.py b/task_module.py" in diff


def test_solution_diff_excludes_test_file():
    """Regression for #54: the grading test must NOT be packed into the gold
    patch — it ships under tests/ so non-oracle agents can reach it."""
    diff = make_solution_diff(task_module_code="x = 1\n")
    assert "test_r2e" not in diff
    assert diff.count("new file mode") == 1
    assert diff.count("--- /dev/null") == 1


def test_solution_diff_hunk_line_counts():
    diff = make_solution_diff(task_module_code="line1\nline2\nline3\n")
    assert "@@ -0,0 +1,3 @@" in diff


def test_solution_diff_handles_missing_trailing_newline():
    """If a file doesn't end with \\n, we emit the `\\ No newline at end of file` line."""
    diff = make_solution_diff(task_module_code="x = 1")  # no trailing newline
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
