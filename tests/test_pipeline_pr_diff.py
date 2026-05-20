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
from repo2rlenv.pipelines.pr_diff import _build_instruction, _strip_info_leak


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
    assert "Submit a unified diff" in instr
