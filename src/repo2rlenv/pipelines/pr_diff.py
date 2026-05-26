"""PR-diff mining (SWE-RL-style) → Harbor-runnable env.

Generation is text-only (no sandbox): for each merged PR within scope:
  1. Pull metadata (title, body, base/head SHAs, files)
  2. Pull the unified diff via `gh pr diff`
  3. Skip if it touches too many files (likely a refactor) or is empty
  4. Build instruction text (issue/PR description rewritten to drop "Closes #...")
  5. Emit a Harbor task

The *emitted* task is runnable in Docker (when ``emit_harbor_env=True``,
the default): it ships a thin ``python:3.12-slim`` ``environment/Dockerfile``
+ a ``tests/test.sh`` carrying the 6-component diff-similarity verifier
(``_pr_diff_verifier``). Reward kind is ``diff_similarity``; the component
breakdown lands in ``/logs/verifier/reward.json``. With
``emit_harbor_env=False`` the task is pure text (instruction.md +
solution/patch.diff) for consumers who score the diff themselves.

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

import base64
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
        f"The repository is checked out at base commit `{pr.base_sha[:12]}`. "
        f"Edit files in place; the verifier captures your changes via "
        f"`git diff` and scores them against an oracle patch using "
        f"SWE-RL-style diff-similarity reward."
    )


# ---------------------------------------------------------------------------
# Harbor env construction (SWE-RL-style diff-similarity verifier)
# ---------------------------------------------------------------------------


def _verifier_source() -> str:
    """Read the standalone verifier module's source for base64 embedding.

    Kept as a separate module (``_pr_diff_verifier.py``) so the verifier is
    reviewable + unit-testable as ordinary Python instead of an opaque
    base64 blob. We read it at gen time and bake into the test.sh.
    """
    verifier_path = Path(__file__).parent / "_pr_diff_verifier.py"
    return verifier_path.read_text(encoding="utf-8")


def build_pr_diff_environment_dockerfile(
    *, repo_url: str, base_commit: str, oracle_diff: str, instruction: str
) -> str:
    """Build the minimal Harbor environment/Dockerfile for a pr_diff task.

    No bootstrap LLM agent — just python:3.12-slim + git + the repo checked
    out at ``base_commit``. The oracle diff, the instruction, AND the
    verifier source are all base64-baked into the image so the verifier
    runs offline with only the Anthropic API call (for the LLM judge) as
    its outbound dep.

    Private repos: the clone uses an optional ``GITHUB_TOKEN`` build arg.
    When the consumer builds a private-repo task they pass
    ``--build-arg GITHUB_TOKEN=$TOKEN``; the clone goes through an
    ``x-access-token`` URL and the remote is immediately scrubbed back to
    the clean URL so the token never lands in ``git config`` inside the
    image. Public repos need no arg. The token is a *build-time* secret,
    never baked into a layer (the ARG default is empty and the remote is
    reset right after clone).

    Why not bootstrap: pr_diff doesn't need a runnable test suite — its
    reward function is text-similarity + LLM-as-judge. A bare python+git
    image keeps the build under ~30 s per cell.
    """
    encoded_oracle = base64.b64encode(oracle_diff.encode("utf-8")).decode("ascii")
    encoded_instruction = base64.b64encode(instruction.encode("utf-8")).decode("ascii")
    encoded_verifier = base64.b64encode(_verifier_source().encode("utf-8")).decode("ascii")
    # Build the authenticated clone URL by injecting the build-arg token
    # right after the scheme. repo_url is always https://github.com/<o>/<r>.git.
    authed_url = repo_url.replace(
        "https://github.com/", "https://x-access-token:${GITHUB_TOKEN}@github.com/", 1
    )
    return (
        "# Auto-generated by Repo2RLEnv pr_diff — 6-component reward env.\n"
        "# Agent-agnostic: the agent (claude-code / openhands / codex / etc.)\n"
        "# installs itself at run time via its harbor adapter. We only ship\n"
        "# the source repo + verifier files needed to score the agent's edits.\n"
        "FROM python:3.12-slim\n"
        # Optional build-time token for private repos. Empty by default →
        # public clone. Never persisted: the remote is scrubbed post-clone.
        "ARG GITHUB_TOKEN=\n"
        # Toolchain layer — cacheable across all pr_diff tasks. Just the
        # minimal kit the verifier needs to capture `git diff` and run
        # python3. No agent-specific tooling.
        "RUN apt-get update \\\n"
        " && apt-get install -y --no-install-recommends "
        "git ca-certificates curl \\\n"
        " && rm -rf /var/lib/apt/lists/*\n"
        "RUN git config --global --add safe.directory /workspace \\\n"
        " && git config --global init.defaultBranch main \\\n"
        " && git config --global advice.detachedHead false\n"
        # Per-repo / per-commit / per-task layers below — cache-miss is OK.
        # Use the token-authed URL when GITHUB_TOKEN is set (private repos),
        # then reset the remote to the clean URL so no credential persists.
        f'RUN if [ -n "$GITHUB_TOKEN" ]; then \\\n'
        f"        git clone --filter=blob:none {authed_url} /workspace; \\\n"
        f"    else \\\n"
        f"        git clone --filter=blob:none {repo_url} /workspace; \\\n"
        f"    fi \\\n"
        f" && git -C /workspace remote set-url origin {repo_url}\n"
        "WORKDIR /workspace\n"
        f"RUN git fetch --depth 1 origin {base_commit} 2>/dev/null \\\n"
        "    || git fetch --unshallow origin 2>/dev/null || true\n"
        f"RUN git reset --hard {base_commit} \\\n"
        " && git clean -fdx -e .venv -e venv -e __pycache__\n"
        "RUN mkdir -p /verifier\n"
        # Bake the oracle diff, instruction, and verifier source so the
        # container is fully self-contained (only the LLM-judge step
        # requires outbound network).
        f'RUN echo "{encoded_oracle}" | base64 -d > /verifier/oracle.patch\n'
        f'RUN echo "{encoded_instruction}" | base64 -d > /verifier/instruction.md\n'
        f'RUN echo "{encoded_verifier}" | base64 -d > /verifier/verifier.py\n'
    )


def build_pr_diff_eval_script(*, base_commit: str) -> str:
    """Build the tests/test.sh that Harbor runs after the agent's edits.

    Thin shim — the 6-component reward logic lives in
    ``/verifier/verifier.py`` (baked into the image by the Dockerfile).
    This script just:

      1. Captures the agent's edits via ``git diff <base_commit>``
      2. Invokes the verifier, which writes ``/logs/verifier/reward.txt``
         (single float) and ``/logs/verifier/reward.json`` (component
         breakdown) for Harbor + downstream inspection.

    Exit code: 0 always — the reward score is the verdict, not the bash
    exit code. The verifier never raises; failures degrade gracefully
    (e.g. LLM-judge network error → that component is null, weight is
    redistributed across the deterministic 5).
    """
    return (
        "#!/bin/bash\n"
        "set -uxo pipefail\n"
        "cd /workspace\n"
        "git config --global --add safe.directory /workspace\n"
        "mkdir -p /logs/verifier\n"
        # Capture the agent's edits as a unified diff against base_commit.
        # IMPORTANT: `git add -A` stages new (untracked) files. Without this
        # step, `git diff` would skip any file the agent created from scratch
        # — which silently misses the file_targeting / region_overlap / size
        # signal for PRs that introduce new files (a very common case).
        "git add -A\n"
        f"git diff --cached {base_commit} > /tmp/predicted.patch\n"
        ": 'START_VERIFY_OUTPUT'\n"
        "python3 /verifier/verifier.py \\\n"
        "    /verifier/oracle.patch \\\n"
        "    /tmp/predicted.patch \\\n"
        "    /verifier/instruction.md\n"
        ": 'END_VERIFY_OUTPUT'\n"
        # Always exit 0 — verifier writes reward.txt; bash exit code is moot
        "exit 0\n"
    )


# ---------------------------------------------------------------------------
# Gen-time helpers: quality filter, baseline calibration, difficulty bucket
# ---------------------------------------------------------------------------


_TEST_PATH_RE = re.compile(r"(?:^|/)(?:tests?|__tests__|test_[^/]+\.py)/|^test_|_test\.")
_DOC_PATH_RE = re.compile(r"(?:^|/)(?:docs?|examples?)/|\.(?:md|rst|txt)$", re.IGNORECASE)


def _diff_changed_files(diff: str) -> list[str]:
    """Extract b/ file paths from a unified diff."""
    out: list[str] = []
    seen: set[str] = set()
    for line in diff.splitlines():
        m = re.match(r"^diff --git a/(\S+) b/(\S+)$", line)
        if m:
            p = m.group(2)
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def _diff_loc_changed(diff: str) -> int:
    """Count real +/- lines (excluding +++ / --- file markers)."""
    n = 0
    for line in diff.splitlines():
        if (line.startswith("+") or line.startswith("-")) and not line.startswith(("+++ ", "--- ")):
            n += 1
    return n


def _quality_filter(pr: PullRequestSummary, diff: str, options: PRDiffOptions) -> str | None:
    """Heuristic gates: drop PRs that obviously make bad RL-env tasks.

    Returns a short skip_reason on failure, or None to keep.
    """
    files = _diff_changed_files(diff)
    if not files:
        return "no_files_in_diff"

    # 100% test-file changes — no app code to fix
    if all(_TEST_PATH_RE.search(f) for f in files):
        return "test_only_diff"

    # 100% docs / markdown
    if all(_DOC_PATH_RE.search(f) for f in files):
        return "docs_only_diff"

    # Reverts — the "fix" is to undo something, not solve a real bug
    if (pr.title or "").lstrip().startswith(("Revert ", "revert ")):
        return "revert_pr"

    # Trivially small diffs — usually not meaningful tasks (3 +/- lines or fewer)
    loc = _diff_loc_changed(diff)
    if loc < options.min_loc_changed:
        return "diff_too_small"

    # Empty / very-short instruction AFTER strip — agent has nothing to act on
    cleaned = _strip_info_leak((pr.body or "").strip())
    title_words = len((pr.title or "").split())
    if not cleaned and title_words < 5:
        return "instruction_too_thin"

    return None


def _compute_no_op_baseline(oracle_diff: str) -> float:
    """Compute the reward an EMPTY predicted diff would get against this oracle.

    Used for per-task calibration (stamped in `task.toml.metadata`).
    Downstream consumers compute ``calibrated = (raw - baseline) / (1 - baseline)``
    so scores are comparable across tasks of different sizes.

    Uses the SAME formula as the in-container verifier (deterministic
    components only — LLM judge is excluded since it depends on API
    availability). Empty predicted → format_valid=0, file_targeting=0,
    region_overlap=0, similarity=0, size_sanity=0 → 0.0 baseline before
    judge weight redistribution.

    With the default 6-component weights AND llm_judge missing → the
    weights renormalize over the 5 deterministic. All 5 score 0 for an
    empty diff → baseline = 0.0. The function returns 0.0 today, but
    the structure is kept so reweighting (Phase 7a.5) auto-updates the
    baseline if non-trivial deterministic components become possible.
    """
    from repo2rlenv.pipelines._pr_diff_verifier import (
        _DEFAULT_WEIGHTS,
        combine,
        file_targeting,
        format_valid,
        region_overlap,
        similarity,
        size_sanity,
    )

    components: dict[str, float | None] = {
        "format_valid": format_valid(""),
        "size_sanity": size_sanity(oracle_diff, ""),
        "file_targeting": file_targeting(oracle_diff, ""),
        "region_overlap": region_overlap(oracle_diff, ""),
        "similarity": similarity(oracle_diff, ""),
        "llm_judge": None,
    }
    return combine(components, _DEFAULT_WEIGHTS)


def _difficulty_for(oracle_diff: str) -> tuple[int, str]:
    """Return (loc_changed, difficulty_bucket) for the oracle diff.

    Buckets:
        trivial : <=  5  +/- lines
        small   :   6-20
        medium  :  21-80
        large   :   81+
    """
    loc = _diff_loc_changed(oracle_diff)
    if loc <= 5:
        return loc, "trivial"
    if loc <= 20:
        return loc, "small"
    if loc <= 80:
        return loc, "medium"
    return loc, "large"


class PRDiffPipeline:
    """PR-diff mining: sandbox-free generation, Docker-runnable output.

    Implements the `Pipeline` Protocol.
    """

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

            # Quality filter: drop candidates that obviously make weak tasks
            # (test-only, docs-only, reverts, trivially-small diffs).
            q_reason = _quality_filter(pr, diff, self.options)
            if q_reason:
                skip_reasons[q_reason] = skip_reasons.get(q_reason, 0) + 1
                self._emit_progress(pr_label, "skip", q_reason)
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
            "pipeline_version": "0.3.0",
            "repo": f"{owner}/{name}",
            "ref": pr.base_sha,
            "reference": pr.url,
            "source_access": self.input.repo.access,
            "built_at": datetime.now(UTC).isoformat(),
            # Spec reward kind is `diff_similarity` (see docs/reference/SPEC.md).
            # The 6-component breakdown is *how* the similarity is scored — it's
            # surfaced in /logs/verifier/reward.json, not as a separate kind.
            "reward_kinds": ["diff_similarity"],
            **({"synthesis_llm": self.input.llm.qualified_name} if self.input.llm else {}),
            "pr_diff": {
                "pr_merged_at": pr.merged_at,
                "diff_format": self.options.diff_format,
                "context_files": pr.changed_files,
            },
        }

        # Compute calibration baseline + difficulty at gen time so consumers
        # can normalize / filter without re-running the verifier.
        instruction_text = _build_instruction(pr)
        baseline = _compute_no_op_baseline(diff)
        loc_changed, difficulty = _difficulty_for(diff)
        repo2env["reward_calibration"] = {
            "baseline_reward": round(baseline, 6),
            "loc_changed": loc_changed,
            "difficulty": difficulty,
        }

        repo_url = f"https://github.com/{owner}/{name}.git"
        dockerfile: str | None = None
        eval_script: str | None = None
        if self.options.emit_harbor_env:
            dockerfile = build_pr_diff_environment_dockerfile(
                repo_url=repo_url,
                base_commit=pr.base_sha,
                oracle_diff=diff,
                instruction=instruction_text,
            )
            eval_script = build_pr_diff_eval_script(base_commit=pr.base_sha)

        return HarborTask(
            name=task_id,
            org=self.input.output.org,
            description=pr.title or task_id,
            instruction=instruction_text,
            oracle_diff=diff,
            repo2env=repo2env,
            difficulty=difficulty if difficulty != "trivial" else "easy",
            category="bugfix",
            keywords=[name, "pr_diff"],
            environment_dockerfile=dockerfile,
            test_script=eval_script,
        )
