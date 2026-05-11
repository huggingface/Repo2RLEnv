"""Sandbox-verified PR mining (SWE-bench-style).

For each merged PR within scope:
  1. Pull metadata + unified diff via `gh pr list` / `gh pr diff`
  2. Split the diff into `patch` (source files) and `test_patch` (test files)
     using the same keyword-on-path heuristic SWE-bench uses
  3. If validation enabled: run the bootstrap container twice (once with
     test_patch only, once with both patches) to compute FAIL_TO_PASS and
     PASS_TO_PASS sets — the verified oracle
  4. Emit a Harbor task with environment/Dockerfile (FROM <bootstrap_image>),
     tests/test.sh (the eval script), and solution/patch.diff (gold patch)

Unlike `pr_diff`, this pipeline requires a working Docker image from the
bootstrap phase. `cmd_generate` triggers `ensure_bootstrap()` automatically
when `requires_bootstrap=True`.

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
This pipeline mirrors the data-collection + validation approach of:

  SWE-bench: Can Language Models Resolve Real-world Github Issues?
  (Jimenez et al., ICLR '24, arXiv:2310.06770)
  https://github.com/SWE-bench/SWE-bench        (MIT)

  SWE-bench-Live: A Live Benchmark for Issue Resolving
  (Zhang et al., NIPS '25, arXiv:2505.23419)
  https://github.com/microsoft/SWE-bench-Live   (MIT)

We adapt the patch-split heuristic (collect/utils.py:extract_patches),
the eval-script structure (harness/test_spec/utils.py:make_eval_script_list_common),
and the F2P/P2P grading semantics (harness/grading.py). No code is copied;
we don't depend on the `swebench` PyPI package.

Released under Apache-2.0 along with the rest of Repo2RLEnv.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from repo2rlenv.auth import resolve_github_token
from repo2rlenv.bootstrap.spec import BootstrapResult
from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.github import (
    GitHubError,
    PullRequestSummary,
    fetch_pr_diff,
    list_merged_prs,
)
from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.spec.input import GenerationInput, PipelineName
from repo2rlenv.spec.options import PRRuntimeOptions

logger = logging.getLogger(__name__)


_CLOSES_RE = re.compile(
    r"\b(?:closes|fixes|resolves)\s+#\d+\b", re.IGNORECASE
)

# Keywords matched on hunk file paths to decide "is this a test file?"
# (Mirrors SWE-bench's collect/utils.py:extract_patches)
_TEST_PATH_KEYWORDS = ("test", "tests", "e2e", "testing")


def _path_is_test(path: str) -> bool:
    """True if the file path looks like a test file by SWE-bench's heuristic."""
    if not path:
        return False
    # Match on any component containing a test keyword (case-insensitive)
    lower = path.lower()
    return any(kw in lower for kw in _TEST_PATH_KEYWORDS)


# Match `diff --git a/<path> b/<path>` block boundaries to split a unified diff
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)


def split_patch_and_test_patch(unified_diff: str) -> tuple[str, str]:
    """Split a PR's unified diff into (source patch, test patch).

    SWE-bench rule: a file hunk goes into `test_patch` iff its path contains
    one of `test/tests/e2e/testing`; everything else goes into `patch`.

    We walk the diff by `diff --git` markers (one per file in the PR) so we
    keep each file's hunks intact.
    """
    if not unified_diff.strip():
        return "", ""

    # Find each "diff --git a/X b/Y" header and the byte offset where its block starts
    matches = list(_DIFF_HEADER_RE.finditer(unified_diff))
    if not matches:
        # Empty / malformed — return whole thing as patch
        return unified_diff, ""

    patch_parts: list[str] = []
    test_parts: list[str] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(unified_diff)
        block = unified_diff[start:end]
        path_a = m.group(1)
        path_b = m.group(2)
        # If EITHER path looks like a test file (covers renames, new files), it's a test
        if _path_is_test(path_a) or _path_is_test(path_b):
            test_parts.append(block)
        else:
            patch_parts.append(block)

    return "".join(patch_parts), "".join(test_parts)


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
        f"The task's test suite verifies your patch by applying it on top of "
        f"the base commit `{pr.base_sha[:12]}` and running the modified tests."
    )


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


def build_eval_script(base_commit: str, test_patch: str, test_cmds: list[str]) -> str:
    """Build the `tests/test.sh` content that Harbor runs after the model patch.

    Adapted from SWE-bench's `harness/test_spec/utils.py:make_eval_script_list_common`.
    The flow:
      1. cd /workspace + mark safe.directory (for non-root git operations)
      2. Reset test files to base_commit (so re-running stays clean)
      3. Apply the test_patch (via heredoc + git apply --reject)
      4. Run test_cmds bracketed with START_TEST_OUTPUT / END_TEST_OUTPUT markers
         so the log parser knows where tests started
      5. Reset test files again on the way out

    The model's predicted patch is applied by Harbor *before* this script runs.
    """
    test_files = _files_in_patch(test_patch)
    heredoc = "EOF_R2E_TEST_PATCH"
    reset = (
        f"git checkout {base_commit} -- {' '.join(test_files)}"
        if test_files
        else "echo 'no test files to reset'"
    )
    apply = f"git apply --verbose --reject - <<'{heredoc}'\n{test_patch}\n{heredoc}"
    test_block = " && ".join(test_cmds) if test_cmds else "echo 'no test_cmds configured'"
    return (
        "#!/bin/bash\n"
        "set -uxo pipefail\n"
        "cd /workspace\n"
        "git config --global --add safe.directory /workspace\n"
        f"{reset} || true\n"   # tolerate test files that didn't exist at base
        f"{apply}\n"
        ": 'START_TEST_OUTPUT'\n"
        f"{test_block}\n"
        ": 'END_TEST_OUTPUT'\n"
        f"{reset} || true\n"
    )


def _files_in_patch(unified_diff: str) -> list[str]:
    """Extract the unique 'b/' file paths touched by a unified diff."""
    if not unified_diff.strip():
        return []
    seen: list[str] = []
    for m in _DIFF_HEADER_RE.finditer(unified_diff):
        b = m.group(2)
        if b not in seen:
            seen.append(b)
    return seen


class PRRuntimePipeline:
    """Sandbox-verified PR mining. Implements the `Pipeline` Protocol."""

    name: ClassVar[PipelineName] = PipelineName.PR_RUNTIME
    requires_bootstrap: ClassVar[bool] = True

    def __init__(
        self,
        input: GenerationInput,
        options: PRRuntimeOptions,
        bootstrap: BootstrapResult | None = None,
    ):
        if bootstrap is None:
            raise RuntimeError(
                "pr_runtime requires a BootstrapResult (set requires_bootstrap=True "
                "and let cmd_generate trigger it, or pass one explicitly)"
            )
        self.input = input
        self.options = options
        self.bootstrap = bootstrap
        self._progress_cb = None

    def set_progress_callback(self, cb) -> None:
        self._progress_cb = cb

    def _emit_progress(self, name: str, outcome: str, reason: str = "") -> None:
        if self._progress_cb is not None:
            try:
                self._progress_cb(name=name, outcome=outcome, reason=reason)
            except Exception as exc:
                logger.debug("progress callback failed: %s", exc)

    # ----- run loop -----------------------------------------------------------

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
        sandbox = None  # lazy-init for the validation loop

        try:
            for pr in prs:
                pr_label = f"{owner}/{name}#{pr.number}"

                # Pre-validation skip filters
                reason = self._pre_filter(pr)
                if reason:
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    self._emit_progress(pr_label, "skip", reason)
                    continue

                # Fetch diff
                try:
                    diff = fetch_pr_diff(owner, name, pr.number, token=token)
                except GitHubError as exc:
                    logger.warning("PR #%d: diff fetch failed: %s", pr.number, exc)
                    skip_reasons["diff_fetch_failed"] = skip_reasons.get("diff_fetch_failed", 0) + 1
                    self._emit_progress(pr_label, "error", "diff_fetch_failed")
                    continue

                patch, test_patch = split_patch_and_test_patch(diff)
                if not patch.strip():
                    skip_reasons["empty_source_patch"] = skip_reasons.get("empty_source_patch", 0) + 1
                    self._emit_progress(pr_label, "skip", "empty_source_patch")
                    continue
                if not test_patch.strip():
                    skip_reasons["no_test_patch"] = skip_reasons.get("no_test_patch", 0) + 1
                    self._emit_progress(pr_label, "skip", "no_test_patch")
                    continue

                # Lite-style structural filters
                lite_reason = self._lite_filter(pr, patch)
                if lite_reason:
                    skip_reasons[lite_reason] = skip_reasons.get(lite_reason, 0) + 1
                    self._emit_progress(pr_label, "skip", lite_reason)
                    continue

                # Validation (optional via skip_validation)
                fail_to_pass: list[str] = []
                pass_to_pass: list[str] = []
                validation_status = "skipped"
                if not self.options.skip_validation:
                    if sandbox is None:
                        sandbox = self._start_validation_sandbox()
                    from repo2rlenv.pipelines.pr_runtime_validate import validate_pr

                    outcome = validate_pr(
                        sandbox=sandbox,
                        base_commit=pr.base_sha,
                        patch=patch,
                        test_patch=test_patch,
                        test_cmds=self.bootstrap.test_cmds,
                        timeout=self.options.validation_timeout_sec,
                    )
                    fail_to_pass = outcome.fail_to_pass
                    pass_to_pass = outcome.pass_to_pass
                    validation_status = outcome.status
                    if (
                        self.options.require_fail_to_pass
                        and len(fail_to_pass) < self.options.min_fail_to_pass
                    ):
                        skip_reasons["no_fail_to_pass"] = skip_reasons.get("no_fail_to_pass", 0) + 1
                        self._emit_progress(pr_label, "skip", outcome.reason or "no_fail_to_pass")
                        continue

                # Emit the Harbor task
                task = self._build_task(
                    pr, patch, test_patch,
                    fail_to_pass=fail_to_pass,
                    pass_to_pass=pass_to_pass,
                    validation_status=validation_status,
                )
                write_harbor_task(task, out_dir)
                emitted += 1
                logger.info("emitted task %s (F2P=%d, P2P=%d)",
                             task.name, len(fail_to_pass), len(pass_to_pass))
                self._emit_progress(task.name, "emit")
        finally:
            if sandbox is not None:
                sandbox.cleanup()

        return PipelineResult(
            candidates=len(prs),
            emitted=emitted,
            skipped=sum(skip_reasons.values()),
            out_dir=out_dir,
            skip_reasons=skip_reasons,
        )

    # ----- filters ------------------------------------------------------------

    def _pre_filter(self, pr: PullRequestSummary) -> str | None:
        """Cheap filters that don't need the diff."""
        if pr.is_draft and self.options.skip_drafts:
            return "draft"
        if not pr.merged_at:
            return "not_merged"
        if not pr.changed_files:
            return "no_files"
        if self.options.min_problem_statement_words > 0:
            if _word_count(pr.body or "") < self.options.min_problem_statement_words:
                return "problem_statement_too_short"
        return None

    def _lite_filter(self, pr: PullRequestSummary, source_patch: str) -> str | None:
        """SWE-bench Lite-style structural filters."""
        source_files = _files_in_patch(source_patch)
        if len(source_files) > self.options.max_source_files_per_pr:
            return "too_many_source_files"
        if self.options.lite_filter:
            if len(source_files) != 1:
                return "lite_not_single_source_file"
            if _word_count(pr.body or "") < 40:
                return "lite_problem_too_short"
            # Reject if PR body contains images / external links / cross-PR/issue refs
            body = (pr.body or "").lower()
            if re.search(r"!\[[^\]]*\]\(|<img\s", body):
                return "lite_has_image"
            if re.search(r"\bhttps?://(?!github\.com/)", body):
                return "lite_has_external_link"
            if re.search(r"\b[a-f0-9]{7,40}\b", body):
                return "lite_has_commit_sha"
        return None

    # ----- sandbox -----------------------------------------------------------

    def _start_validation_sandbox(self):
        """Spin up a DockerSandbox from the bootstrap image (shared across PRs)."""
        from repo2rlenv.bootstrap.docker import DockerSandbox
        # The bootstrap image already contains the repo at the bootstrap-time HEAD.
        # We pass repo_dir=None? No — DockerSandbox.start requires a repo_dir to copy in.
        # The repo is already in the image; we'll just `git checkout` to each PR's base_commit
        # from inside the container, so the path here is a no-op marker.
        import tempfile
        marker = Path(tempfile.mkdtemp(prefix="r2e-pr-runtime-"))
        (marker / ".keep").write_text("")  # docker cp <src>/. <dst> works on any non-empty dir
        # Pull just the tag, don't re-copy the repo (image already has it)
        sandbox = DockerSandbox.start(
            base_image=self.bootstrap.image_tag,
            repo_dir=marker,
            platform=self.input.bootstrap.platform,
        )
        return sandbox

    # ----- task builder -------------------------------------------------------

    def _build_task(
        self,
        pr: PullRequestSummary,
        patch: str,
        test_patch: str,
        *,
        fail_to_pass: list[str],
        pass_to_pass: list[str],
        validation_status: str,
    ) -> HarborTask:
        owner, name = self.input.repo.owner_name
        task_id = f"{owner}__{name}-{pr.number}"

        eval_script = build_eval_script(
            base_commit=pr.base_sha,
            test_patch=test_patch,
            test_cmds=self.bootstrap.test_cmds,
        )
        dockerfile = (
            f"# Auto-generated by Repo2RLEnv pr_runtime\n"
            f"FROM {self.bootstrap.image_digest}\n"
            f"WORKDIR /workspace\n"
        )

        repo2env = {
            "pipeline": "pr_runtime",
            "pipeline_version": "0.3.0",
            "repo": f"{owner}/{name}",
            "ref": pr.base_sha,
            "reference": pr.url,
            "source_access": self.input.repo.access,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "synthesis_llm": self.input.llm.qualified_name,
            "reward_kinds": ["test_execution", "diff_similarity"],
            "pr_runtime": {
                "pr_url": pr.url,
                "pr_merged_at": pr.merged_at,
                "base_commit": pr.base_sha,
                "fail_to_pass": fail_to_pass,
                "pass_to_pass": pass_to_pass,
                "validation_status": validation_status,
                "bootstrap_image": self.bootstrap.image_digest,
            },
        }

        return HarborTask(
            name=task_id,
            org=self.input.output.org,
            description=pr.title or task_id,
            instruction=_build_instruction(pr),
            oracle_diff=patch,
            repo2env=repo2env,
            difficulty="medium",
            category="bugfix",
            keywords=[name, "pr_runtime"],
            environment_dockerfile=dockerfile,
            test_script=eval_script,
        )
