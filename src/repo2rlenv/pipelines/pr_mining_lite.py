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
from datetime import datetime, timezone
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
from repo2rlenv.spec.options import PRMiningLiteOptions

logger = logging.getLogger(__name__)


_CLOSES_RE = re.compile(
    r"\b(?:closes|fixes|resolves)\s+#\d+\b", re.IGNORECASE
)


def _build_instruction(pr: PullRequestSummary) -> str:
    """Strip 'Closes #N' style boilerplate from the PR description."""
    body = (pr.body or "").strip()
    body = _CLOSES_RE.sub("", body).strip()
    if not body:
        body = "(no description provided in source PR)"
    return (
        f"# Issue\n\n"
        f"**Title:** {pr.title}\n\n"
        f"## Description\n\n"
        f"{body}\n\n"
        f"## Task\n\n"
        f"Modify the repository so that the issue described above is resolved. "
        f"Submit a unified diff against the repository at base commit "
        f"`{pr.base_sha[:12]}`."
    )


class PRMiningLitePipeline:
    """No-sandbox, text-only PR mining. Implements the `Pipeline` Protocol."""

    name: ClassVar[PipelineName] = PipelineName.PR_MINING_LITE

    def __init__(self, input: GenerationInput, options: PRMiningLiteOptions):
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
                owner, name,
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
            "pipeline": "pr_mining_lite",
            "pipeline_version": "0.1.0",
            "repo": f"{owner}/{name}",
            "ref": pr.base_sha,
            "reference": pr.url,
            "source_access": self.input.repo.access,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "synthesis_llm": self.input.llm.qualified_name,
            "pr_mining_lite": {
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
            keywords=[name, "pr_mining_lite"],
        )
