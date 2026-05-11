"""commit_runtime — metadata filters, instruction synthesis, contract.

The git_local helpers are tested in test_git_local.py against a real
tiny in-tree git repo. Here we focus on the pipeline's pure-Python logic:

  - metadata filter (skip merges, bots, short messages)
  - structural filter (CI-only, too many files, no new test funcs)
  - instruction synthesis from commit fields
  - pipeline contract (requires_bootstrap, name)
"""

from __future__ import annotations

import pytest

from repo2rlenv.git_local import CommitInfo
from repo2rlenv.pipelines.commit_runtime import (
    CommitRuntimePipeline,
    _strip_commit_prefix,
    build_instruction_from_commit,
)
from repo2rlenv.spec.options import CommitRuntimeOptions


def _make_commit(
    sha: str = "a" * 40,
    parents: list[str] | None = None,
    author_email: str = "alice@example.com",
    subject: str = "fix: real bugfix in foo",
    body: str = "Closes #42.\nReplaces the broken thing with the working thing.",
) -> CommitInfo:
    parents = parents if parents is not None else ["b" * 40]
    return CommitInfo(
        sha=sha,
        parent_sha=parents[0] if parents else "",
        parents=parents,
        author_name="Alice",
        author_email=author_email,
        authored_at="2026-05-12T08:15:22Z",
        subject=subject,
        body=body,
    )


# -------------------------- _strip_commit_prefix ------------------------------


def test_strip_commit_prefix_fix():
    assert _strip_commit_prefix("fix: thing") == "thing"


def test_strip_commit_prefix_feat_scoped():
    assert _strip_commit_prefix("feat(api): new thing") == "new thing"


def test_strip_commit_prefix_case_insensitive():
    assert _strip_commit_prefix("FIX: thing") == "thing"


def test_strip_commit_prefix_passthrough_when_no_prefix():
    assert _strip_commit_prefix("plain old subject line") == "plain old subject line"


# -------------------------- build_instruction_from_commit ---------------------


def test_instruction_uses_subject_and_body():
    commit = _make_commit()
    out = build_instruction_from_commit(commit)
    assert "**Title:** real bugfix in foo" in out  # `fix:` stripped
    assert "Replaces the broken thing" in out
    assert commit.parent_sha[:12] in out


def test_instruction_strips_closes_trailers():
    commit = _make_commit(body="Closes #42.\nThis is the actual context.")
    out = build_instruction_from_commit(commit)
    assert "Closes #42" not in out
    assert "actual context" in out


def test_instruction_handles_empty_body():
    commit = _make_commit(body="")
    out = build_instruction_from_commit(commit)
    assert "**Title:**" in out
    assert "## Task" in out
    # No empty description section
    assert "## Description\n\n\n" not in out


# -------------------------- metadata filter -----------------------------------


def _pipeline_for_filter_tests(**opts):
    """Build a pipeline whose _metadata_filter/_structural_filter we can call."""
    from unittest.mock import MagicMock

    pipe = CommitRuntimePipeline.__new__(CommitRuntimePipeline)
    pipe.options = CommitRuntimeOptions(**opts)
    pipe.bootstrap = MagicMock()
    pipe.input = MagicMock()
    pipe._progress_cb = None
    return pipe


def test_metadata_filter_skip_merge_commit():
    pipe = _pipeline_for_filter_tests(skip_merge_commits=True)
    merge = _make_commit(parents=["a" * 40, "b" * 40])  # 2 parents
    assert pipe._metadata_filter(merge) == "merge_commit"


def test_metadata_filter_keeps_merge_when_option_disabled():
    pipe = _pipeline_for_filter_tests(skip_merge_commits=False)
    merge = _make_commit(parents=["a" * 40, "b" * 40])
    assert pipe._metadata_filter(merge) is None


def test_metadata_filter_excluded_author():
    pipe = _pipeline_for_filter_tests(exclude_authors=["dependabot[bot]@example.com"])
    commit = _make_commit(author_email="dependabot[bot]@example.com")
    assert pipe._metadata_filter(commit) == "excluded_author"


def test_metadata_filter_short_message():
    """`min_message_words=5` rejects 1-3-word commits like 'wip'."""
    pipe = _pipeline_for_filter_tests(min_message_words=5)
    commit = _make_commit(subject="wip", body="")
    assert pipe._metadata_filter(commit) == "short_message"


def test_metadata_filter_passes_a_good_commit():
    pipe = _pipeline_for_filter_tests()
    assert pipe._metadata_filter(_make_commit()) is None


# -------------------------- structural filter ---------------------------------


def test_structural_filter_skips_ci_only():
    pipe = _pipeline_for_filter_tests(skip_ci_only=True)
    source = """\
diff --git a/.github/workflows/test.yml b/.github/workflows/test.yml
@@ -1 +1 @@
-uses: v1
+uses: v2
"""
    test_patch = "diff --git a/tests/test_x.py b/tests/test_x.py\n@@ +1 @@\n+def test_y(): pass\n"
    assert pipe._structural_filter(source, test_patch) == "ci_only_patch"


def test_structural_filter_too_many_source_files():
    pipe = _pipeline_for_filter_tests(max_source_files_per_commit=2)
    source = "\n".join(
        f"diff --git a/src/file{i}.py b/src/file{i}.py\n@@ +1 @@\n+x = 1" for i in range(5)
    )
    test_patch = "diff --git a/tests/test_x.py b/tests/test_x.py\n@@ +1 @@\n+def test_y(): pass\n"
    assert pipe._structural_filter(source, test_patch) == "too_many_source_files"


def test_structural_filter_no_new_test_funcs():
    pipe = _pipeline_for_filter_tests(require_new_test_funcs=True)
    source = "diff --git a/src/foo.py b/src/foo.py\n@@ +1 @@\n+x = 1"
    # Test patch only adjusts comments — no new test funcs
    test_patch = "diff --git a/tests/test_x.py b/tests/test_x.py\n@@ -1 +1 @@\n-# old\n+# new\n"
    assert pipe._structural_filter(source, test_patch) == "no_new_test_funcs"


def test_structural_filter_passes_good_commit():
    pipe = _pipeline_for_filter_tests()
    source = "diff --git a/src/foo.py b/src/foo.py\n@@ +1 @@\n+x = 1"
    test_patch = "diff --git a/tests/test_x.py b/tests/test_x.py\n@@ +1 @@\n+def test_y(): pass\n"
    assert pipe._structural_filter(source, test_patch) is None


# -------------------------- pipeline contract ---------------------------------


def test_commit_runtime_requires_bootstrap_attr():
    assert CommitRuntimePipeline.requires_bootstrap is True


def test_commit_runtime_rejects_missing_bootstrap():
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
        pipeline=PipelineSpec(name=PipelineName.COMMIT_RUNTIME, options={}),
        llm=LLMSpec(provider="anthropic", model="claude-sonnet-4-6"),
        output=OutputSpec(destination="./out", org="x", dataset_name="y"),
    )
    with pytest.raises(RuntimeError, match="requires a BootstrapResult"):
        CommitRuntimePipeline(gen_input, CommitRuntimeOptions(), bootstrap=None)
