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


def test_metadata_filter_rejects_non_bugfix_types():
    """chore/docs/feat/refactor/style/test/ci/build/perf/revert are NOT bugfixes."""
    pipe = _pipeline_for_filter_tests()
    for prefix in (
        "chore: bump",
        "docs: typo fix",
        "feat(api): new endpoint",
        "refactor: rename helper",
        "style: black format",
        "test: add coverage",
        "ci: pin actions",
        "build: drop py3.10",
        "perf: hot loop tweak",
        "revert: revert previous change",
    ):
        c = _make_commit(subject=prefix, body="long enough body text here")
        assert pipe._metadata_filter(c) == "non_bugfix_type", prefix


def test_metadata_filter_no_bugfix_signal_is_rejected():
    """No `fix:` prefix, no `Closes #N`, no bugfix keyword ⇒ rejected."""
    pipe = _pipeline_for_filter_tests()
    c = _make_commit(subject="Update README with new logo", body="")
    assert pipe._metadata_filter(c) == "no_bugfix_signal"


def test_metadata_filter_keeps_fix_prefix():
    pipe = _pipeline_for_filter_tests()
    c = _make_commit(subject="fix: off-by-one in pager", body="")
    assert pipe._metadata_filter(c) is None


def test_metadata_filter_keeps_linked_issue_even_without_keyword():
    """A commit that links an issue (e.g. `Closes #42`) is a bugfix signal."""
    pipe = _pipeline_for_filter_tests()
    c = _make_commit(subject="Update token handling", body="Closes #42, see context.")
    assert pipe._metadata_filter(c) is None


def test_metadata_filter_keeps_bugfix_keyword_in_subject():
    pipe = _pipeline_for_filter_tests()
    c = _make_commit(subject="Repair broken parser regression on Windows", body="")
    assert pipe._metadata_filter(c) is None


# -------------------------- instruction leak strip ----------------------------


def test_instruction_strips_fix_pr_link_leak():
    """Markdown PR/commit links in the body must not leak into the instruction."""
    commit = _make_commit(
        body=(
            "This bug was first noticed in [#1234](https://github.com/o/r/pull/1234) "
            "and the fix is in commit 0123456789abcdef0123456789abcdef01234567. "
            "Replaces the broken thing."
        ),
    )
    out = build_instruction_from_commit(commit)
    assert "1234" not in out
    assert "0123456789abcdef" not in out
    assert "Replaces the broken thing" in out  # context preserved


def test_instruction_strips_leak_from_subject():
    """Cross-references in the subject itself must also be stripped."""
    commit = _make_commit(subject="fix: regression introduced in [#42](url)", body="")
    out = build_instruction_from_commit(commit)
    assert "#42" not in out


def test_instruction_uses_issue_when_provided():
    """When `issue=(title, body)` is supplied (issue-fetch fallback), the
    problem statement comes from the issue — not the leak-prone commit msg."""
    commit = _make_commit(
        subject="fix: off-by-one in `Pager.advance`",  # leaks the function name
        body="See [#42](https://github.com/o/r/issues/42)",
    )
    issue = (
        "Pagination skips the last entry",
        "When you click 'next' on the final page, the trailing entry vanishes.",
    )
    out = build_instruction_from_commit(commit, issue=issue)
    # Issue text drives the prompt
    assert "Pagination skips the last entry" in out
    assert "trailing entry vanishes" in out
    # And the commit-message leak ("Pager.advance", "off-by-one") does NOT appear
    assert "Pager.advance" not in out
    assert "off-by-one" not in out


def test_instruction_reflows_long_body_with_template_noise():
    """Verbose commit bodies get the same `_reflow_pr_body` cleanup as PRs:
    drop HTML template comments, stop at checklist headers, collapse blanks."""
    commit = _make_commit(
        body=(
            "<!-- thanks for the contribution -->\n"
            "Real problem statement: parser crashes on empty input.\n\n\n\n"
            "## Checklist\n"
            "- [ ] tests added\n"
            "- [ ] docs updated\n"
        )
    )
    out = build_instruction_from_commit(commit)
    assert "Real problem statement" in out
    assert "thanks for the contribution" not in out
    # Checklist noise stripped from the description
    assert "tests added" not in out


# -------------------------- _build_task metadata ------------------------------


def _stub_pipeline_for_build_task(test_cmds=None, language="python"):
    """A pipeline instance with just enough scaffolding to call `_build_task`."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    pipe = CommitRuntimePipeline.__new__(CommitRuntimePipeline)
    pipe.options = CommitRuntimeOptions()
    pipe.bootstrap = SimpleNamespace(
        image_tag="local/r2e-bootstrap/o__r:abc",
        image_digest="local/r2e-bootstrap/o__r:abc",
        pushed_to_registry=False,
        test_cmds=test_cmds or ["pytest -v"],
        language=SimpleNamespace(value=language),
    )
    pipe.input = MagicMock()
    pipe.input.repo.owner_name = ("o", "r")
    pipe.input.repo.access = "auto"
    pipe.input.repo.url = "https://github.com/o/r"
    pipe.input.output.org = "default"
    pipe.input.llm = None
    pipe.input.bootstrap.platform = "linux/amd64"
    pipe._progress_cb = None
    return pipe


def test_build_task_stamps_reward_calibration_and_difficulty():
    """Tasks now carry reward_calibration parity with pr_runtime — needed by
    the manifest enricher and the eval_grade flag."""
    pipe = _stub_pipeline_for_build_task()
    commit = _make_commit(subject="fix: parser crashes on empty input")
    # one-line patch + one new F2P test ⇒ trivial bucket
    patch = (
        "diff --git a/parser.py b/parser.py\n"
        "--- a/parser.py\n"
        "+++ b/parser.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-old\n"
        "+new\n"
    )
    test_patch = (
        "diff --git a/tests/test_parser.py b/tests/test_parser.py\n"
        "--- a/tests/test_parser.py\n"
        "+++ b/tests/test_parser.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+def test_empty_input():\n"
        "+    assert True\n"
    )
    task = pipe._build_task(
        commit,
        patch,
        test_patch,
        fail_to_pass=["tests/test_parser.py::test_empty_input"],
        pass_to_pass=["tests/test_parser.py::test_other"],
        validation_status="ok",
    )
    cal = task.repo2env["reward_calibration"]
    assert cal["f2p_count"] == 1
    assert cal["p2p_count"] == 1
    assert cal["source_files"] == 1
    assert cal["loc_changed"] >= 1
    assert cal["difficulty"] in {"trivial", "small", "medium", "large"}
    # The HarborTask's difficulty is set from the bucket, not hard-coded "medium"
    assert task.difficulty == cal["difficulty"]


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
