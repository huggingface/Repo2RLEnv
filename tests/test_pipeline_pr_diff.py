"""Tests for `pipelines.pr_diff` — instruction-text construction.

Focus is on the v0.8.3 Arc 1 optimization: broaden the info-leak strip
so PR descriptions don't hint at the patch the agent is supposed to
produce. We strip:

  - Closes / Fixes / Resolves #N  (including multi like "Closes #1, #2")
  - See / Refs / Follow-up to #N
  - Markdown issue links `[#N](url)`
  - Bare github.com /pull/, /issues/, /commit/ URLs
  - Trailer lines (Co-authored-by, Signed-off-by, Reviewed-by, Acked-by)
  - Squash-merge "(#N)" suffix in the title
"""

from __future__ import annotations

from repo2rlenv.github import PullRequestSummary
from repo2rlenv.pipelines.pr_diff import (
    _build_instruction,
    _strip_info_leak,
    build_pr_diff_environment_dockerfile,
    build_pr_diff_eval_script,
)


def _pr(*, title: str = "Fix the bug", body: str = "") -> PullRequestSummary:
    """Build a minimal PullRequestSummary stub."""
    return PullRequestSummary(
        number=1,
        title=title,
        body=body,
        state="closed",
        merged_at="2026-01-01T00:00:00Z",
        base_ref="main",
        base_sha="0" * 40,
        head_sha="1" * 40,
        is_draft=False,
        url="https://github.com/example/repo/pull/1",
        changed_files=["a.py"],
    )


# ---------------------------------------------------------------------------
# _strip_info_leak
# ---------------------------------------------------------------------------


def test_strips_closes_fixes_resolves() -> None:
    out = _strip_info_leak("Fix it.  Closes #42")
    assert "Closes" not in out
    assert "#42" not in out
    assert "Fix it" in out


def test_strips_multi_closes() -> None:
    out = _strip_info_leak("rolls up everything. Fixes #1, #2, #3 — and that's all.")
    assert "#1" not in out and "#2" not in out and "#3" not in out
    assert "Fixes" not in out
    assert "rolls up everything" in out


def test_strips_see_refs_follow_up() -> None:
    cases = [
        "See #99",
        "Refs #99",
        "ref #99",
        "see also #99",
        "Follow-up to #99",
        "follow up to #99",
    ]
    for c in cases:
        out = _strip_info_leak(f"Context. {c}. Done.")
        assert "#99" not in out, f"failed to strip {c!r} → {out!r}"


def test_strips_markdown_issue_link() -> None:
    out = _strip_info_leak("Background in [#1234](https://github.com/x/y/issues/1234).")
    assert "#1234" not in out
    assert "https://github.com" not in out
    assert "Background in" in out


def test_strips_bare_github_pull_url() -> None:
    cases = [
        "https://github.com/foo/bar/pull/42",
        "https://github.com/foo/bar/issues/42",
        "https://github.com/foo/bar/commit/abc123def456",
    ]
    for url in cases:
        out = _strip_info_leak(f"See {url} for details.")
        assert "github.com" not in out, f"failed for {url}"


def test_strips_trailer_lines() -> None:
    body = (
        "The real bug is in the parser.\n"
        "\n"
        "Co-authored-by: Someone Else <e@example.com>\n"
        "Signed-off-by: Approver <a@example.com>\n"
        "Reviewed-by: Reviewer <r@example.com>\n"
    )
    out = _strip_info_leak(body)
    assert "Co-authored-by" not in out
    assert "Signed-off-by" not in out
    assert "Reviewed-by" not in out
    assert "The real bug is in the parser" in out


def test_keeps_legitimate_text() -> None:
    body = (
        "Users report that `Choice('a','b').convert('c')` raises.\n"
        "It should raise BadParameter not a plain ValueError."
    )
    # No leak patterns → body should pass through verbatim (modulo trailing trim)
    out = _strip_info_leak(body)
    assert out == body


def test_squeezes_whitespace_after_strip() -> None:
    body = "Useful text.\n\n\n\nMore useful text."
    out = _strip_info_leak(body)
    # Triple+ blank lines collapse to a single blank line
    assert "\n\n\n" not in out


# ---------------------------------------------------------------------------
# _build_instruction
# ---------------------------------------------------------------------------


def test_squash_suffix_stripped_from_title() -> None:
    pr = _pr(title="Fix the bug (#1234)", body="")
    instr = _build_instruction(pr)
    assert "**Title:** Fix the bug\n" in instr
    assert "(#1234)" not in instr


def test_title_drops_parenthesized_closes_marker() -> None:
    """Real-world title from stretchr/testify#1888:
    "assert: fix NotSubset error messages using %#v instead of %q (fixes #1800)"
    """
    pr = _pr(
        title="assert: fix NotSubset error messages using %#v instead of %q (fixes #1800)",
        body="",
    )
    instr = _build_instruction(pr)
    assert "fixes #1800" not in instr
    assert "#1800" not in instr
    assert "assert: fix NotSubset" in instr


def test_strips_redirect_github_url() -> None:
    """Dependabot release notes commonly embed `https://redirect.github.com/...` —
    these still leak the answer.
    """
    body = "Pulls in github-script fixes via https://redirect.github.com/x/y/pull/1929 — done."
    out = _strip_info_leak(body)
    assert "redirect.github.com" not in out
    assert "pull/1929" not in out


def test_strips_closes_with_markdown_issue_link() -> None:
    """`Closes [#1234](url)` — the markdown-link form of a Closes/Fixes ref.

    Before the fix, _CLOSES_RE only handled bare `Closes #N`, so the
    markdown-link form left the `Closes ` keyword orphaned in the output.
    """
    body = "Background prose. Closes [#1234](https://github.com/x/y/issues/1234). More."
    out = _strip_info_leak(body)
    assert "Closes" not in out
    assert "#1234" not in out
    assert "github.com" not in out
    assert "Background prose" in out
    assert "More." in out


def test_strips_multi_closes_with_markdown_links() -> None:
    """`Fixes [#1](url), [#2](url)` — closes-list of markdown-link refs."""
    body = (
        "Refactors the parser. "
        "Fixes [#1](https://github.com/x/y/issues/1), [#2](https://github.com/x/y/issues/2). End."
    )
    out = _strip_info_leak(body)
    assert "Fixes" not in out
    assert "#1" not in out and "#2" not in out
    assert "github.com" not in out
    assert "Refactors the parser" in out


def test_strips_descriptive_markdown_gh_link() -> None:
    """`[some descriptive text](https://github.com/x/y/pull/N)` — markdown link
    whose link-text isn't `[#N]` but whose URL still leaks the answer.

    Before the fix, the bare-URL regex stripped the URL but left orphaned
    `[some descriptive text]()` brackets behind.
    """
    body = (
        "There's a deep discussion in "
        "[my detailed analysis](https://github.com/x/y/pull/1234) you should read."
    )
    out = _strip_info_leak(body)
    assert "github.com" not in out
    assert "pull/1234" not in out
    # No orphaned empty markdown brackets
    assert "[" not in out and "]()" not in out
    assert "There's a deep discussion in" in out


def test_strips_see_with_markdown_issue_link() -> None:
    body = "Context here. See [#42](https://github.com/x/y/issues/42)."
    out = _strip_info_leak(body)
    assert "See" not in out
    assert "#42" not in out
    assert "Context here." in out


def test_empty_body_emits_placeholder() -> None:
    pr = _pr(title="No description here", body="")
    instr = _build_instruction(pr)
    assert "(no description provided in source PR)" in instr


def test_body_only_links_collapses_to_placeholder() -> None:
    """If the body is just a closes-link and nothing else, we still emit
    the placeholder rather than an empty Description section."""
    pr = _pr(title="Trivial", body="Closes #99")
    instr = _build_instruction(pr)
    assert "(no description provided in source PR)" in instr


def test_full_instruction_shape() -> None:
    pr = _pr(
        title="Fix Choice.convert to raise BadParameter",
        body=(
            "The current implementation raises ValueError. "
            "Closes #1234. See https://github.com/pallets/click/issues/1234 for context."
        ),
    )
    instr = _build_instruction(pr)
    assert "# Issue" in instr
    assert "Fix Choice.convert" in instr
    assert "Closes #1234" not in instr
    assert "github.com" not in instr
    assert "## Task" in instr
    # New harbor-runnable task — agent edits files directly; verifier captures
    # changes via git diff and scores them with SWE-RL-style similarity.
    assert "Edit files in place" in instr
    assert "diff-similarity" in instr


# ---------------------------------------------------------------------------
# build_pr_diff_environment_dockerfile + build_pr_diff_eval_script
# ---------------------------------------------------------------------------


def test_dockerfile_starts_from_python_slim() -> None:
    df = build_pr_diff_environment_dockerfile(
        repo_url="https://github.com/pallets/click.git",
        base_commit="abc1234567890",
        oracle_diff="diff --git a/x.py b/x.py\n",
        instruction="# Issue\n\nfix the thing",
    )
    assert "FROM python:3.12-slim" in df
    assert "apt-get install" in df and "git" in df
    assert "git clone --filter=blob:none https://github.com/pallets/click.git /workspace" in df
    assert "git reset --hard abc1234567890" in df


def test_dockerfile_bakes_oracle_diff_as_base64() -> None:
    import base64

    oracle = "diff --git a/foo.py b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
    df = build_pr_diff_environment_dockerfile(
        repo_url="https://github.com/x/y.git",
        base_commit="deadbeef",
        oracle_diff=oracle,
        instruction="anything",
    )
    encoded = base64.b64encode(oracle.encode("utf-8")).decode("ascii")
    assert encoded in df
    assert "base64 -d > /verifier/oracle.patch" in df


def test_dockerfile_bakes_instruction_and_verifier_source() -> None:
    import base64

    instr = "# Issue\nTitle: Fix the thing"
    df = build_pr_diff_environment_dockerfile(
        repo_url="https://github.com/x/y.git",
        base_commit="deadbeef",
        oracle_diff="diff --git a/x b/x\n",
        instruction=instr,
    )
    encoded_instr = base64.b64encode(instr.encode("utf-8")).decode("ascii")
    assert encoded_instr in df
    assert "base64 -d > /verifier/instruction.md" in df
    assert "base64 -d > /verifier/verifier.py" in df


def test_dockerfile_handles_oracle_with_special_chars() -> None:
    """A patch containing quotes / $ / backticks must base64-encode cleanly."""
    oracle = 'diff --git a/q.py b/q.py\n+x = "$y `cmd` $(other)"\n'
    df = build_pr_diff_environment_dockerfile(
        repo_url="https://github.com/x/y.git",
        base_commit="cafe",
        oracle_diff=oracle,
        instruction="anything",
    )
    # No raw special chars from the patch should appear in the Dockerfile —
    # they're only in the base64 blob.
    assert "$y `cmd`" not in df


def test_eval_script_shebang_and_paths() -> None:
    es = build_pr_diff_eval_script(base_commit="abc1234567890")
    assert es.startswith("#!/bin/bash")
    # `git add -A` BEFORE `git diff --cached <base>` ensures new
    # (untracked) files added by the agent are captured in the diff —
    # without this, PRs that add files would silently downscore.
    assert "git add -A" in es
    assert "git diff --cached abc1234567890 > /tmp/predicted.patch" in es
    # The thin shim just invokes the baked-in verifier
    assert "/verifier/verifier.py" in es
    assert "/verifier/oracle.patch" in es
    assert "/verifier/instruction.md" in es


def test_eval_script_exits_zero() -> None:
    """Verifier writes reward.txt; bash exit code is moot."""
    es = build_pr_diff_eval_script(base_commit="abc")
    assert "exit 0" in es


def test_dockerfile_supports_private_repo_build_arg() -> None:
    """The emitted Dockerfile clones via an optional GITHUB_TOKEN build arg
    so private repos work at consumer build time, then scrubs the remote so
    the token never persists in git config inside the image."""
    df = build_pr_diff_environment_dockerfile(
        repo_url="https://github.com/myorg/private-repo.git",
        base_commit="abc123",
        oracle_diff="diff --git a/x b/x\n+1\n",
        instruction="do it",
    )
    # Build arg declared, empty default (public repos need no arg).
    assert "ARG GITHUB_TOKEN=" in df
    # Authed clone uses the x-access-token form when the arg is set.
    assert "x-access-token:${GITHUB_TOKEN}@github.com/myorg/private-repo.git" in df
    # Public fallback clone is the clean URL.
    assert "git clone --filter=blob:none https://github.com/myorg/private-repo.git" in df
    # Remote is reset to the clean URL so the token can't leak via git config.
    assert "remote set-url origin https://github.com/myorg/private-repo.git" in df
    # The token itself must never appear literally baked anywhere.
    assert "ghp_" not in df
