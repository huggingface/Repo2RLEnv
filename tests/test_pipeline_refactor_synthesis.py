"""refactor_synthesis pipeline — contract + helpers."""

from __future__ import annotations

import pytest

from repo2rlenv.pipelines.refactor_synthesis import (
    RefactorSynthesisPipeline,
    _build_instruction,
    build_rename_eval_script,
)
from repo2rlenv.spec.options import RefactorSynthesisOptions

# ---------------------------------------------------------------------------
# build_rename_eval_script
# ---------------------------------------------------------------------------


def test_eval_script_includes_structural_grep_for_old():
    s = build_rename_eval_script(
        test_cmds=["pytest -v"],
        old_name="old_func",
        new_name="new_func",
        require_old_gone=True,
        require_new_present=True,
    )
    assert "grep" in s
    assert "old_func" in s
    assert "new_func" in s
    assert "STRUCT_FAIL" in s


def test_eval_script_skips_old_check_when_disabled():
    s = build_rename_eval_script(
        test_cmds=["pytest -v"],
        old_name="old_func",
        new_name="new_func",
        require_old_gone=False,
        require_new_present=True,
    )
    # Should still grep for the new name, but not the old
    assert "STRUCTURAL FAIL: old name" not in s
    assert "STRUCTURAL FAIL: new name" in s


def test_eval_script_writes_reward_file():
    s = build_rename_eval_script(
        test_cmds=["pytest"],
        old_name="x",
        new_name="y",
        require_old_gone=True,
        require_new_present=True,
    )
    assert "/logs/verifier/reward.txt" in s
    assert "1.0" in s and "0.0" in s


def test_eval_script_includes_path_prelude_for_go():
    s = build_rename_eval_script(
        test_cmds=["go test ./..."],
        old_name="x",
        new_name="y",
        require_old_gone=True,
        require_new_present=True,
        language="go",
    )
    assert "/usr/local/go/bin" in s


# ---------------------------------------------------------------------------
# _build_instruction
# ---------------------------------------------------------------------------


def _commit_fixture():
    from repo2rlenv.git_local import CommitInfo

    return CommitInfo(
        sha="a" * 40,
        parent_sha="b" * 40,
        parents=["b" * 40],
        author_name="A",
        author_email="a@example.com",
        authored_at="2026-04-01T00:00:00Z",
        subject="rename foo to bar",
        body="",
    )


def test_instruction_includes_both_names():
    instr = _build_instruction(
        old_name="foo",
        new_name="bar",
        kind="function",
        commit=_commit_fixture(),
        require_old_gone=False,
    )
    assert "`foo`" in instr
    assert "`bar`" in instr
    assert "function" in instr
    assert _commit_fixture().sha[:12] in instr


def test_instruction_uses_symbol_when_kind_missing():
    instr = _build_instruction(
        old_name="foo",
        new_name="bar",
        kind="",
        commit=_commit_fixture(),
        require_old_gone=False,
    )
    assert "symbol" in instr


def test_instruction_mentions_shim_acceptable_when_old_gone_false():
    instr = _build_instruction(
        old_name="foo",
        new_name="bar",
        kind="",
        commit=_commit_fixture(),
        require_old_gone=False,
    )
    assert "shim is acceptable" in instr


def test_instruction_strict_when_old_gone_true():
    instr = _build_instruction(
        old_name="foo",
        new_name="bar",
        kind="",
        commit=_commit_fixture(),
        require_old_gone=True,
    )
    assert "should remain in the source tree" in instr


# ---------------------------------------------------------------------------
# Pipeline contract
# ---------------------------------------------------------------------------


def test_refactor_synthesis_requires_bootstrap_attr():
    assert RefactorSynthesisPipeline.requires_bootstrap is True


def test_refactor_synthesis_rejects_missing_bootstrap():
    from repo2rlenv.spec.input import (
        GenerationInput,
        LLMSpec,
        OutputSpec,
        PipelineName,
        PipelineSpec,
        RepoSpec,
    )

    gen_input = GenerationInput(
        repo=RepoSpec(url="pallets/click"),
        pipeline=PipelineSpec(name=PipelineName.REFACTOR_SYNTHESIS, options={}),
        llm=LLMSpec(provider="anthropic", model="claude-sonnet-4-6"),
        output=OutputSpec(destination="./out", org="x", dataset_name="y"),
    )
    with pytest.raises(RuntimeError, match="requires a BootstrapResult"):
        RefactorSynthesisPipeline(gen_input, RefactorSynthesisOptions(), bootstrap=None)


def test_refactor_synthesis_options_defaults():
    opts = RefactorSynthesisOptions()
    assert opts.limit == 50
    assert opts.clone_depth == 200
    # require_old_name_gone defaults False — real-world renames keep shims
    assert opts.require_old_name_gone is False
    assert opts.require_new_name_present is True
    assert opts.skip_merge_commits is True
