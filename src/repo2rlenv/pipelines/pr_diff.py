"""Text-only PR mining (SWE-RL-style).

For each merged PR within scope:
  1. Pull metadata (title, body, base/head SHAs, files)
  2. Pull the unified diff via `gh pr diff`
  3. Skip if it touches too many files (likely a refactor) or is empty
  4. Build instruction text (issue/PR description rewritten to drop "Closes #...")
  5. Emit a Harbor task: instruction.md + solution/patch.diff

No Docker. No tests. Verifier = diff similarity (consumer applies our reward
function or SWE-RL's, against the oracle).

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
The "text-only PR-as-task with diff-similarity reward" pattern is inspired by:

  SWE-RL: Advancing LLM Reasoning via Reinforcement Learning on Open Software
  Evolution (Wei et al., NeurIPS '25, arXiv:2502.18449)
  https://github.com/facebookresearch/swe-rl    (CC BY-NC 4.0)

The PR-mining task formulation is also inherited from:

  SWE-bench: Can Language Models Resolve Real-world Github Issues?
  (Jimenez et al., 2024)
  https://github.com/SWE-bench/SWE-bench        (MIT)

This file is an independent implementation. No code is copied from either
project; the GitHub-API access path uses the `gh` CLI directly. Released
under Apache-2.0 along with the rest of Repo2RLEnv.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from repo2rlenv.auth import resolve_github_token
from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.github import (
    GitHubError,
    PullRequestSummary,
    fetch_pr_diff,
    list_merged_prs,
)
from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.spec.input import GenerationInput, PipelineName
from repo2rlenv.spec.options import PRDiffOptions

logger = logging.getLogger(__name__)


# GitHub-issue / GitHub-PR linkbacks that hint at the solution. We strip
# them from instruction text so the agent can't shortcut by fetching the
# linked artifact.
#
# Patterns come in two shapes for each kind: a "bare" form (`Closes #N`,
# `https://github.com/.../pull/N`) and a markdown-link form (`Closes
# [#N](url)`, `[descriptive text](https://github.com/.../pull/N)`). The
# composite forms must run BEFORE the piece-wise ones so we don't leave
# orphaned `Closes ` keywords or empty `[text]()` brackets behind.
_CLOSES_RE = re.compile(r"\b(?:closes|fixes|resolves)\s+#\d+(?:\s*,\s*#\d+)*\b", re.IGNORECASE)
_REFS_RE = re.compile(
    r"\b(?:see(?:\s+also)?|refs?|follow[- ]?up\s+(?:to|of))\s+#\d+(?:\s*,\s*#\d+)*\b",
    re.IGNORECASE,
)
# "Closes [#1234](url)" / "Fixes [#1](url), [#2](url)" — same keyword set
# as _CLOSES_RE / _REFS_RE but with markdown-link issue refs.
_CLOSES_MD_RE = re.compile(
    r"\b(?:closes|fixes|resolves)\s+\[#\d+\]\([^)]+\)(?:\s*,\s*\[#\d+\]\([^)]+\))*",
    re.IGNORECASE,
)
_REFS_MD_RE = re.compile(
    r"\b(?:see(?:\s+also)?|refs?|follow[- ]?up\s+(?:to|of))\s+"
    r"\[#\d+\]\([^)]+\)(?:\s*,\s*\[#\d+\]\([^)]+\))*",
    re.IGNORECASE,
)
_MD_ISSUE_LINK_RE = re.compile(r"\[#\d+\]\([^)]+\)")
# Markdown link whose URL points at a GH pull/issues/commit, with arbitrary
# link text. We strip the whole `[text](url)` construct so the prose doesn't
# end up with empty `[text]()` brackets after the bare-URL strip below.
_MD_GH_URL_RE = re.compile(
    r"\[[^\]]+\]\(https?://(?:[a-z0-9.-]+\.)?github\.com/[^)]+/(?:pull|issues|commit)/[^)]+\)",
    re.IGNORECASE,
)
# Matches github.com proper + common GH-proxy redirector hosts (Dependabot
# release notes embed `redirect.github.com`, GitLab mirrors use
# `mirror.github.com`, etc.).
_GH_URL_RE = re.compile(
    r"https?://(?:[a-z0-9.-]+\.)?github\.com/[^\s)\"<']+/(?:pull|issues|commit)/[^\s)\"<']+",
    re.IGNORECASE,
)
_TRAILER_LINE_RE = re.compile(
    r"^(?:Co-authored-by|Signed-off-by|Reviewed-by|Acked-by):.*$",
    re.IGNORECASE | re.MULTILINE,
)
# Squash-merge suffix on titles. Two flavors:
#   1. " (#1234)" — GitHub's default squash suffix
#   2. " (fixes #1234)" — manual close-style marker
_SQUASH_SUFFIX_RE = re.compile(
    r"\s*\((?:(?:closes|fixes|resolves|see|refs?)\s+)?#\d+(?:\s*,\s*#\d+)*\)\s*$",
    re.IGNORECASE,
)


def _strip_info_leak(body: str) -> str:
    """Remove patterns that hint at the patch the agent should produce.

    The goal: leave the natural-language problem description intact, but
    drop linkbacks (Closes/See/follow-up #N + bare GH URLs + squash suffixes
    + commit trailers) that point to the answer.

    Order matters: composite patterns (`Closes [#N](url)`, `[text](gh-url)`)
    are stripped BEFORE the piece-wise patterns so we don't leave orphaned
    `Closes ` keywords or empty `[text]()` markdown brackets behind.
    """
    body = _CLOSES_MD_RE.sub("", body)
    body = _REFS_MD_RE.sub("", body)
    body = _MD_GH_URL_RE.sub("", body)
    body = _CLOSES_RE.sub("", body)
    body = _REFS_RE.sub("", body)
    body = _MD_ISSUE_LINK_RE.sub("", body)
    body = _GH_URL_RE.sub("", body)
    body = _TRAILER_LINE_RE.sub("", body)
    # Squeeze whitespace left behind by deletions
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def _build_instruction(pr: PullRequestSummary) -> str:
    """Strip leakage patterns from the PR body + title; emit issue-style prose."""
    title = _SQUASH_SUFFIX_RE.sub("", (pr.title or "").strip())
    body = _strip_info_leak((pr.body or "").strip())
    if not body:
        body = "(no description provided in source PR)"
    return (
        f"# Issue\n\n"
        f"**Title:** {title}\n\n"
        f"## Description\n\n"
        f"{body}\n\n"
        f"## Task\n\n"
        f"Modify the repository so that the issue described above is resolved. "
        f"Submit a unified diff against the repository at base commit "
        f"`{pr.base_sha[:12]}`."
    )


class PRDiffPipeline:
    """No-sandbox, text-only PR mining. Implements the `Pipeline` Protocol."""

    name: ClassVar[PipelineName] = PipelineName.PR_DIFF
    requires_bootstrap: ClassVar[bool] = False

    def __init__(self, input: GenerationInput, options: PRDiffOptions, bootstrap=None):
        # bootstrap is unused for pr_diff — accepted for Protocol uniformity
        self.input = input
        self.options = options
        self._progress_cb = None  # set via set_progress_callback for live UI

    def set_progress_callback(self, cb) -> None:
        """Wire a per-candidate callback so a CLI live view can update.

        Callable signature: cb(name: str, outcome: "emit"|"skip"|"error", reason: str = "")
        """
        self._progress_cb = cb

    def _emit_progress(self, name: str, outcome: str, reason: str = "") -> None:
        if self._progress_cb is not None:
            try:
                self._progress_cb(name=name, outcome=outcome, reason=reason)
            except Exception as exc:
                logger.debug("progress callback failed: %s", exc)

    def run(self, out_dir: Path) -> PipelineResult:
        out_dir.mkdir(parents=True, exist_ok=True)

        token = resolve_github_token(self.input.repo, self.input.auth)
        if self.input.repo.access == "private" and not token:
            raise RuntimeError(
                "private repo specified but no GitHub token resolved. "
                "Run `gh auth login` or set GITHUB_TOKEN."
            )

        owner, name = self.input.repo.owner_name
        logger.info("listing merged PRs for %s/%s (limit=%d)", owner, name, self.options.limit)

        try:
            prs = list_merged_prs(
                owner,
                name,
                limit=self.options.limit,
                since=self.options.since,
                until=self.options.until,
                skip_drafts=self.options.skip_drafts,
                token=token,
            )
        except GitHubError as exc:
            raise RuntimeError(f"failed to list PRs: {exc}") from exc

        skip_reasons: dict[str, int] = {}
        emitted = 0

        for pr in prs:
            pr_label = f"{owner}/{name}#{pr.number}"
            reason = self._should_skip(pr)
            if reason:
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                self._emit_progress(pr_label, "skip", reason)
                continue

            try:
                diff = fetch_pr_diff(owner, name, pr.number, token=token)
            except GitHubError as exc:
                logger.warning("PR #%d: diff fetch failed: %s", pr.number, exc)
                skip_reasons["diff_fetch_failed"] = skip_reasons.get("diff_fetch_failed", 0) + 1
                self._emit_progress(pr_label, "error", "diff_fetch_failed")
                continue

            if not diff.strip():
                skip_reasons["empty_diff"] = skip_reasons.get("empty_diff", 0) + 1
                self._emit_progress(pr_label, "skip", "empty_diff")
                continue

            task = self._build_task(pr, diff)
            write_harbor_task(task, out_dir)
            emitted += 1
            logger.info("emitted task %s", task.name)
            self._emit_progress(task.name, "emit")

        return PipelineResult(
            candidates=len(prs),
            emitted=emitted,
            skipped=sum(skip_reasons.values()),
            out_dir=out_dir,
            skip_reasons=skip_reasons,
        )

    def _should_skip(self, pr: PullRequestSummary) -> str | None:
        if pr.is_draft and self.options.skip_drafts:
            return "draft"
        if not pr.changed_files:
            return "no_files"
        if len(pr.changed_files) > self.options.max_files_per_pr:
            return "too_many_files"
        if not pr.merged_at:
            return "not_merged"
        return None

    def _build_task(self, pr: PullRequestSummary, diff: str) -> HarborTask:
        owner, name = self.input.repo.owner_name
        task_id = f"{owner}__{name}-{pr.number}"

        repo2env = {
            "pipeline": "pr_diff",
            "pipeline_version": "0.1.0",
            "repo": f"{owner}/{name}",
            "ref": pr.base_sha,
            "reference": pr.url,
            "source_access": self.input.repo.access,
            "built_at": datetime.now(UTC).isoformat(),
            **({"synthesis_llm": self.input.llm.qualified_name} if self.input.llm else {}),
            "pr_diff": {
                "pr_merged_at": pr.merged_at,
                "diff_format": self.options.diff_format,
                "context_files": pr.changed_files,
            },
        }

        return HarborTask(
            name=task_id,
            org=self.input.output.org,
            description=pr.title or task_id,
            instruction=_build_instruction(pr),
            oracle_diff=diff,
            repo2env=repo2env,
            difficulty="medium",
            category="bugfix",
            keywords=[name, "pr_diff"],
        )
