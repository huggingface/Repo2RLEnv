"""Commit-level mining (SWE-GEN style).

For each commit on the target branch within scope:
  1. Walk `git log` (deeper clone than bootstrap's `--depth=1`)
  2. Filter at the metadata layer (skip merges, bots, short messages,
     too-many-source-files)
  3. `git show <sha>` → split into patch / test_patch using the same
     heuristic as pr_runtime
  4. Validate inside the bootstrap container (reuses pr_runtime's harness)
  5. Emit Harbor task with the same shape as pr_runtime

Unlike `pr_runtime`, there's no PR to link to. Instructions come from
the commit subject + body (after stripping conventional-commit prefixes
and `Closes #N` trailers).

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
Inspired by:

  R2E-Gym: Procedural Environments and Hybrid Verifiers for Scaling
  Open-Weights SWE Agents (Jain et al., COLM '25)
  https://github.com/R2E-Gym/R2E-Gym                            (MIT)

Their "SWE-GEN" curation approach — bypassing PR-review filters and
mining commits directly — informs this pipeline's design. We share their
finding that commit-level mining produces complementary, larger
candidate pools per repo. No code is copied; implementation is original.

Released under Apache-2.0 along with the rest of Repo2RLEnv.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from repo2rlenv.auth import resolve_github_token
from repo2rlenv.bootstrap.runner import _shallow_clone_at_ref
from repo2rlenv.bootstrap.spec import BootstrapResult
from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.git_local import CommitInfo, GitError, list_commits, show_diff
from repo2rlenv.github import fetch_issue
from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.pipelines.pr_runtime import (
    _count_new_test_funcs,
    _diff_loc_changed,
    _difficulty_bucket,
    _files_in_patch,
    _linked_issue_number,
    _reflow_pr_body,
    _runtime_aux_files,
    _strip_info_leak,
    build_environment_dockerfile,
    build_eval_script,
    normalize_test_cmds_for_runtime,
    split_patch_and_test_patch,
    targeted_test_cmds_for_pr,
)
from repo2rlenv.spec.input import GenerationInput, PipelineName
from repo2rlenv.spec.options import CommitRuntimeOptions

logger = logging.getLogger(__name__)


# Strips "Closes #N" / "Fixes [#N](...)" trailers from commit bodies.
# Includes the markdown form Arc 2 added (`fixes [#1234](url)`) and the
# bare `[#N](url)` link form too.
_CLOSES_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+\[?#\d+\]?(?:\([^)]*\))?",
    re.IGNORECASE,
)

# Strips conventional-commit prefixes ("fix: ", "feat(scope): ", etc.) from subjects
_CC_PREFIX_RE = re.compile(
    r"^(?:fix|feat|chore|docs|refactor|test|perf|build|ci|style|revert)(?:\([^)]+\))?:\s*",
    re.IGNORECASE,
)

# Conventional-commit types that are NOT bugfixes. These slip into the
# candidate pool today because `_CC_PREFIX_RE` only *strips* the prefix; it
# doesn't reject. Mirrors `pr_runtime._NON_BUG_TITLE_RE` for the commit world.
_NON_BUG_TYPE_RE = re.compile(
    r"^(?:chore|docs|feat|refactor|style|test|ci|build|perf|revert)(?:\([^)]+\))?:\s*",
    re.IGNORECASE,
)

# A bugfix-positive signal we look for when no `fix:` prefix is present and
# no `Closes #N` trailer links an issue. Avoids letting feature commits with
# no type prefix sail through.
_BUGFIX_KEYWORD_RE = re.compile(
    r"\b(?:fix(?:e[sd])?|fixing|bug(?:fix|s)?|regression|crash(?:e[sd]|ing)?|broken|"
    r"incorrect(?:ly)?|wrong(?:ly)?|fail(?:s|ed|ing|ure)?|defect|hotfix|patch(?:e[sd])?)\b",
    re.IGNORECASE,
)


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


def _files_changed_in_diff(unified_diff: str) -> list[str]:
    """Same `b/` path extractor pr_runtime uses; lifted here for the filter layer."""
    return _files_in_patch(unified_diff)


def _strip_commit_prefix(subject: str) -> str:
    """Drop conventional-commit type prefix from subject, leaving the description."""
    cleaned = _CC_PREFIX_RE.sub("", subject, count=1)
    return cleaned.strip()


def build_instruction_from_commit(
    commit: CommitInfo,
    *,
    issue: tuple[str, str] | None = None,
) -> str:
    """Render a commit's subject + body (or a linked issue) into the task prompt.

    Sourcing order — same lesson as Arc 2's `pr_runtime` fix:
      1. If the commit links an issue (``Closes #N``) and the caller has
         fetched it, use the **issue title + body** as the problem statement.
         The bug report is far less leak-prone than the commit message, which
         frequently names the function being fixed and points at fix-PRs.
      2. Otherwise fall back to the commit subject + body, run through
         `_strip_info_leak` + `_reflow_pr_body` to scrub cross-refs and trim
         template noise / line-wrap chatter.
    """
    if issue is not None:
        i_title, i_body = issue
        title = _strip_info_leak(i_title).strip()
        body = _reflow_pr_body(_strip_info_leak(_CLOSES_RE.sub("", i_body or ""))).strip()
    else:
        title = _strip_info_leak(_strip_commit_prefix(commit.subject)).strip()
        body = _reflow_pr_body(_strip_info_leak(_CLOSES_RE.sub("", commit.body or ""))).strip()
    if not title:
        title = _strip_commit_prefix(commit.subject) or "(no title)"
    parts = [f"# Issue\n\n**Title:** {title}"]
    if body:
        parts.append("## Description\n\n" + body)
    parts.append(
        "## Task\n\n"
        "Modify the repository so that the issue described above is resolved. "
        "The task's test suite verifies your patch by applying it on top of "
        f"the base commit `{commit.parent_sha[:12]}` and running the modified tests."
    )
    return "\n\n".join(parts)


class CommitRuntimePipeline:
    """Commit-level mining with sandbox-verified F2P/P2P oracles."""

    name: ClassVar[PipelineName] = PipelineName.COMMIT_RUNTIME
    requires_bootstrap: ClassVar[bool] = True
    experimental: ClassVar[bool] = True

    def __init__(
        self,
        input: GenerationInput,
        options: CommitRuntimeOptions,
        bootstrap: BootstrapResult | None = None,
    ):
        if bootstrap is None:
            raise RuntimeError(
                "commit_runtime requires a BootstrapResult (set requires_bootstrap=True "
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
        owner_name = f"{owner}/{name}"
        skip_reasons: dict[str, int] = {}
        emitted = 0
        sandbox = None
        candidates: list[CommitInfo] = []

        with tempfile.TemporaryDirectory(prefix="r2e-commit-runtime-") as tmp:
            clone_dir = Path(tmp) / "repo"
            logger.info(
                "cloning %s @ %s (depth=%d) for commit walk",
                self.input.repo.url,
                self.input.repo.ref,
                self.options.clone_depth,
            )
            try:
                _shallow_clone_at_ref(
                    self.input.repo.url,
                    self.input.repo.ref,
                    token,
                    clone_dir,
                    depth=self.options.clone_depth,
                )
            except Exception as exc:
                raise RuntimeError(f"failed to clone {self.input.repo.url}: {exc}") from exc

            try:
                candidates = list_commits(
                    clone_dir,
                    since=self.options.since,
                    until=self.options.until,
                    limit=self.options.limit,
                    branch=self.options.branch,
                )
            except GitError as exc:
                raise RuntimeError(f"git log failed: {exc}") from exc
            logger.info(
                "commit_runtime: %d candidate commits in [%s, %s]",
                len(candidates),
                self.options.since,
                self.options.until,
            )

            try:
                for commit in candidates:
                    label = f"{owner_name}@{commit.sha[:12]}"

                    # Metadata-level filters (cheap)
                    reason = self._metadata_filter(commit)
                    if reason:
                        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                        self._emit_progress(label, "skip", reason)
                        continue

                    # Diff-level filters
                    try:
                        diff = show_diff(clone_dir, commit.sha)
                    except GitError as exc:
                        logger.warning("commit %s: git show failed: %s", commit.sha[:12], exc)
                        skip_reasons["diff_fetch_failed"] = (
                            skip_reasons.get("diff_fetch_failed", 0) + 1
                        )
                        self._emit_progress(label, "error", "diff_fetch_failed")
                        continue

                    patch, test_patch = split_patch_and_test_patch(diff)
                    if not patch.strip():
                        skip_reasons["empty_source_patch"] = (
                            skip_reasons.get("empty_source_patch", 0) + 1
                        )
                        self._emit_progress(label, "skip", "empty_source_patch")
                        continue
                    if not test_patch.strip():
                        skip_reasons["no_test_patch"] = skip_reasons.get("no_test_patch", 0) + 1
                        self._emit_progress(label, "skip", "no_test_patch")
                        continue

                    structural_reason = self._structural_filter(patch, test_patch)
                    if structural_reason:
                        skip_reasons[structural_reason] = skip_reasons.get(structural_reason, 0) + 1
                        self._emit_progress(label, "skip", structural_reason)
                        continue

                    if not commit.parent_sha:
                        skip_reasons["root_commit"] = skip_reasons.get("root_commit", 0) + 1
                        self._emit_progress(label, "skip", "root_commit")
                        continue

                    # Validation
                    fail_to_pass: list[str] = []
                    pass_to_pass: list[str] = []
                    validation_status = "skipped"
                    if not self.options.skip_validation:
                        if sandbox is None:
                            sandbox = self._start_validation_sandbox()
                        from repo2rlenv.pipelines.pr_runtime_validate import validate_pr

                        targeted_cmds = targeted_test_cmds_for_pr(
                            normalize_test_cmds_for_runtime(self.bootstrap.test_cmds),
                            _files_in_patch(test_patch),
                        )
                        outcome = validate_pr(
                            sandbox=sandbox,
                            base_commit=commit.parent_sha,
                            patch=patch,
                            test_patch=test_patch,
                            test_cmds=targeted_cmds,
                            language=self.bootstrap.language.value,
                            timeout=self.options.validation_timeout_sec,
                        )
                        fail_to_pass = outcome.fail_to_pass
                        pass_to_pass = outcome.pass_to_pass
                        validation_status = outcome.status
                        if (
                            self.options.require_fail_to_pass
                            and len(fail_to_pass) < self.options.min_fail_to_pass
                        ):
                            skip_reasons["no_fail_to_pass"] = (
                                skip_reasons.get("no_fail_to_pass", 0) + 1
                            )
                            self._emit_progress(label, "skip", outcome.reason or "no_fail_to_pass")
                            continue

                    # Issue-fetch fallback: when the commit links an issue
                    # (`Closes #N`), source the problem statement from the
                    # issue body — the bug report is far less leak-prone than
                    # the commit message. Same lesson as Arc 2 for pr_runtime.
                    issue_num = _linked_issue_number(commit.message or "")
                    issue = None
                    if issue_num is not None:
                        issue = fetch_issue(owner, name, issue_num, token=token)
                    task = self._build_task(
                        commit,
                        patch,
                        test_patch,
                        fail_to_pass=fail_to_pass,
                        pass_to_pass=pass_to_pass,
                        validation_status=validation_status,
                        issue=issue,
                    )
                    write_harbor_task(task, out_dir)
                    emitted += 1
                    logger.info(
                        "emitted task %s (F2P=%d, P2P=%d)",
                        task.name,
                        len(fail_to_pass),
                        len(pass_to_pass),
                    )
                    self._emit_progress(task.name, "emit")
            finally:
                if sandbox is not None:
                    sandbox.cleanup()
                # tempfile cleanup happens automatically; make sure git's loose
                # objects don't keep ref to the clone
                shutil.rmtree(clone_dir, ignore_errors=True)

        return PipelineResult(
            candidates=len(candidates),
            emitted=emitted,
            skipped=sum(skip_reasons.values()),
            out_dir=out_dir,
            skip_reasons=skip_reasons,
        )

    # ----- filters ------------------------------------------------------------

    def _metadata_filter(self, commit: CommitInfo) -> str | None:
        """Cheap filters that don't need the diff content.

        Bugfix detection runs in two stages: (a) reject conventional-commit
        types that are explicitly not bugfixes (chore/docs/feat/refactor/
        style/test/ci/build/perf/revert), (b) require a positive bugfix
        signal — `fix:` prefix, a `Closes #N` / `Fixes #N` issue trailer,
        or a bugfix keyword in the subject. Together these mirror Arc 2's
        non-bug-PR rejection and stop feature/refactor commits from
        burning bootstrap cycles in validation.
        """
        if self.options.skip_merge_commits and commit.is_merge:
            return "merge_commit"
        if commit.author_email in self.options.exclude_authors:
            return "excluded_author"
        if _word_count(commit.message) < self.options.min_message_words:
            return "short_message"
        if (
            self.options.min_problem_statement_words > 0
            and _word_count(commit.message) < self.options.min_problem_statement_words
        ):
            return "problem_statement_too_short"
        subject = commit.subject or ""
        # (a) explicit non-bugfix type prefix → reject.
        if _NON_BUG_TYPE_RE.match(subject):
            return "non_bugfix_type"
        # (b) at least one bugfix signal must be present.
        has_fix_prefix = bool(re.match(r"^fix(?:\([^)]+\))?:\s*", subject, re.IGNORECASE))
        has_linked_issue = _linked_issue_number(commit.message or "") is not None
        has_keyword = bool(_BUGFIX_KEYWORD_RE.search(subject))
        if not (has_fix_prefix or has_linked_issue or has_keyword):
            return "no_bugfix_signal"
        return None

    def _structural_filter(self, source_patch: str, test_patch: str) -> str | None:
        """Diff-level filters (mirrors pr_runtime._structural_quality_filter)."""
        source_files = _files_changed_in_diff(source_patch)
        if (
            self.options.skip_ci_only
            and source_files
            and all(p.startswith(".github/") for p in source_files)
        ):
            return "ci_only_patch"
        if len(source_files) > self.options.max_source_files_per_commit:
            return "too_many_source_files"
        if self.options.require_new_test_funcs and _count_new_test_funcs(test_patch) < 1:
            return "no_new_test_funcs"
        return None

    # ----- sandbox -----------------------------------------------------------

    def _start_validation_sandbox(self):
        """Spin up a DockerSandbox from the bootstrap image (shared across commits)."""
        from repo2rlenv.bootstrap.docker import DockerSandbox

        marker = Path(tempfile.mkdtemp(prefix="r2e-commit-runtime-"))
        (marker / ".keep").write_text("")
        sandbox = DockerSandbox.start(
            base_image=self.bootstrap.image_tag,
            repo_dir=marker,
            platform=self.input.bootstrap.platform,
        )
        return sandbox

    # ----- task builder -------------------------------------------------------

    def _build_task(
        self,
        commit: CommitInfo,
        patch: str,
        test_patch: str,
        *,
        fail_to_pass: list[str],
        pass_to_pass: list[str],
        validation_status: str,
        issue: tuple[str, str] | None = None,
    ) -> HarborTask:
        owner, name = self.input.repo.owner_name
        # commit_runtime task ID convention: <owner>__<repo>-<sha12>
        # (analogous to pr_runtime's <owner>__<repo>-<pr_number>)
        task_id = f"{owner}__{name}-{commit.sha[:12]}"

        eval_script = build_eval_script(
            base_commit=commit.parent_sha,
            test_patch=test_patch,
            test_cmds=targeted_test_cmds_for_pr(
                normalize_test_cmds_for_runtime(self.bootstrap.test_cmds),
                _files_in_patch(test_patch),
            ),
            language=self.bootstrap.language.value,
            # Without F2P/P2P, build_eval_script returns the binary exit-code
            # script and reward.json is never written. Pass them in so we get
            # the graded path + tracked/command_resolved breakdown.
            fail_to_pass=fail_to_pass,
            pass_to_pass=pass_to_pass,
        )
        image_ref = (
            self.bootstrap.image_digest
            if self.bootstrap.pushed_to_registry
            else self.bootstrap.image_tag
        )
        dockerfile = build_environment_dockerfile(
            bootstrap_image=image_ref,
            base_commit=commit.parent_sha,
        )

        loc_changed = _diff_loc_changed(patch)
        difficulty = _difficulty_bucket(len(fail_to_pass), loc_changed)
        repo2env = {
            "pipeline": "commit_runtime",
            "pipeline_version": "0.8.3",
            "repo": f"{owner}/{name}",
            "ref": commit.parent_sha,
            "reference": f"https://github.com/{owner}/{name}/commit/{commit.sha}",
            "source_access": self.input.repo.access,
            "built_at": datetime.now(UTC).isoformat(),
            **({"synthesis_llm": self.input.llm.qualified_name} if self.input.llm else {}),
            "reward_kinds": ["test_execution", "diff_similarity"],
            "commit_runtime": {
                "commit_sha": commit.sha,
                "parent_sha": commit.parent_sha,
                "authored_at": commit.authored_at,
                "author_email": commit.author_email,
                "subject": commit.subject,
                "fail_to_pass": fail_to_pass,
                "pass_to_pass": pass_to_pass,
                "validation_status": validation_status,
                "bootstrap_image": self.bootstrap.image_digest,
            },
            # Calibration parity with pr_runtime: lets the manifest enricher
            # compute difficulty / p2p_count == 0 / loc_changed sliceability
            # without re-parsing the diff.
            "reward_calibration": {
                "f2p_count": len(fail_to_pass),
                "p2p_count": len(pass_to_pass),
                "source_files": len(_files_in_patch(patch)),
                "loc_changed": loc_changed,
                "difficulty": difficulty,
            },
        }

        return HarborTask(
            name=task_id,
            org=self.input.output.org,
            description=_strip_commit_prefix(commit.subject) or task_id,
            instruction=build_instruction_from_commit(commit, issue=issue),
            oracle_diff=patch,
            repo2env=repo2env,
            difficulty=difficulty,
            category="bugfix",
            keywords=[name, "commit_runtime"],
            environment_dockerfile=dockerfile,
            test_script=eval_script,
            # Ship the graded verifier + F2P/P2P JSON as plain task artifacts
            # (Harbor mounts tests/ at /tests). Same shape pr_runtime ships
            # after Arc 2's plain-artifacts refactor — without this, test.sh
            # falls back to the exit-code reward and reward.json is never
            # written, so tracked/command_resolved + the breakdown are lost.
            aux_files=_runtime_aux_files(fail_to_pass, pass_to_pass) if fail_to_pass else {},
        )
