"""Map OSV vulnerability records to fixing commits in the target repo.

Workflow:
  1. Query OSV's `/v1/query` for the repo's ecosystem + package
  2. For each vuln, scan `references[]` for a github.com/<owner>/<repo>/commit/<sha> URL
  3. Fetch that commit's diff via `gh api`; split into source/test patches
  4. Validation (when test_patch is non-empty): same harness as pr_runtime —
     two-stage F2P/P2P inside the bootstrap container
  5. Emit a Harbor task whose instruction is the CVE description and whose
     oracle is the fix diff

Unlike `pr_runtime` / `commit_runtime`, many CVE fixes don't ship a test
patch in the same commit (the test came later, or the regression was
covered by an existing case). When `test_patch` is empty, we emit the
task with `validation_status="no_test_patch"` — the verifier signal is
just "tests still pass with the fix applied", which is weaker than F2P
but still useful as training data.

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
Inspired by:

  PatchSeeker (Le et al.) — LLM + embedding similarity for NVD→commit mapping
  https://github.com/hungkien05/PatchSeeker        (MIT)

  CVE-Bench (Zhu et al., NAACL '25) — benchmark of CVE-fixing tasks

We use the OSV (Open Source Vulnerabilities) public API instead of NVD
because OSV pre-resolves the `references[]` block to include fix-commit
URLs in a structured way, removing the need for LLM-driven CVE→commit
mapping for the common case. No code is copied; the OSV client is
stdlib-only, the validation harness is reused verbatim from pr_runtime.

Released under Apache-2.0 along with the rest of Repo2RLEnv.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from repo2rlenv.auth import resolve_github_token
from repo2rlenv.bootstrap.spec import BootstrapResult
from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.github import (
    GitHubError,
    fetch_commit_diff,
    fetch_commit_parent,
)
from repo2rlenv.osv import OSVError, OSVVuln, guess_ecosystem, query_vulns, severity_at_least
from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.pipelines.pr_runtime import (
    _files_in_patch,
    build_environment_dockerfile,
    build_eval_script,
    normalize_test_cmds_for_runtime,
    split_patch_and_test_patch,
    targeted_test_cmds_for_pr,
)
from repo2rlenv.spec.input import GenerationInput, PipelineName
from repo2rlenv.spec.options import CVEPatchesOptions

logger = logging.getLogger(__name__)


def _build_instruction(vuln: OSVVuln) -> str:
    """Render the CVE description into a task-prompt-shaped string."""
    title = vuln.summary or vuln.id
    body = vuln.details.strip() or "(no detailed description available)"
    cve = vuln.cve_id or vuln.id
    cwe = ", ".join(vuln.cwe_ids) or "(no CWE tag)"
    return (
        f"# Security advisory: {cve}\n\n"
        f"**Title:** {title}\n\n"
        f"**Severity:** {vuln.severity_text or 'unknown'}\n"
        f"**CWE:** {cwe}\n\n"
        f"## Description\n\n{body}\n\n"
        f"## Task\n\n"
        f"Patch the repository to address the vulnerability described above. "
        f"The task's test suite verifies your patch by applying it on top of "
        f"the parent of the original fixing commit and running the tests."
    )


class CVEPatchesPipeline:
    """OSV-driven CVE → fix-commit pipeline."""

    name: ClassVar[PipelineName] = PipelineName.CVE_PATCHES
    requires_bootstrap: ClassVar[bool] = True

    def __init__(
        self,
        input: GenerationInput,
        options: CVEPatchesOptions,
        bootstrap: BootstrapResult | None = None,
    ):
        if bootstrap is None:
            raise RuntimeError(
                "cve_patches requires a BootstrapResult (set requires_bootstrap=True "
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
        label_root = f"{owner}/{name}"

        ecosystem = self.options.osv_ecosystem or guess_ecosystem(owner)
        package = self.options.osv_package or name.lower()
        logger.info(
            "cve_patches: querying OSV ecosystem=%s package=%s for %s/%s",
            ecosystem,
            package,
            owner,
            name,
        )
        try:
            vulns = query_vulns(package, ecosystem)
        except OSVError as exc:
            raise RuntimeError(f"OSV query failed: {exc}") from exc

        # Filter by severity + must have at least one fix commit
        in_scope: list[tuple[OSVVuln, str]] = []
        for v in vulns:
            if not severity_at_least(v, self.options.min_severity):
                continue
            commits = v.fix_commits(owner=owner, repo=name)
            if not commits:
                continue
            # If multiple commits, pick the first — usually the primary fix
            in_scope.append((v, commits[0]))

        logger.info(
            "cve_patches: %d vulns total, %d in scope (have fix commit in %s/%s)",
            len(vulns),
            len(in_scope),
            owner,
            name,
        )

        skip_reasons: dict[str, int] = {}
        emitted = 0
        sandbox = None

        try:
            for vuln, fix_sha in in_scope:
                if emitted >= self.options.limit:
                    break
                label = f"{label_root}#{vuln.id}"

                # Resolve parent commit
                parent_sha = fetch_commit_parent(owner, name, fix_sha, token=token)
                if not parent_sha:
                    skip_reasons["no_parent_commit"] = skip_reasons.get("no_parent_commit", 0) + 1
                    self._emit_progress(label, "skip", "no_parent_commit")
                    continue

                # Fetch the fix commit's diff
                try:
                    diff = fetch_commit_diff(owner, name, fix_sha, token=token)
                except GitHubError as exc:
                    logger.warning("CVE %s: diff fetch failed: %s", vuln.id, exc)
                    skip_reasons["diff_fetch_failed"] = skip_reasons.get("diff_fetch_failed", 0) + 1
                    self._emit_progress(label, "error", "diff_fetch_failed")
                    continue

                patch, test_patch = split_patch_and_test_patch(diff)
                if not patch.strip():
                    skip_reasons["empty_source_patch"] = (
                        skip_reasons.get("empty_source_patch", 0) + 1
                    )
                    self._emit_progress(label, "skip", "empty_source_patch")
                    continue

                source_files = _files_in_patch(patch)
                if len(source_files) > self.options.max_source_files_per_fix:
                    skip_reasons["too_many_source_files"] = (
                        skip_reasons.get("too_many_source_files", 0) + 1
                    )
                    self._emit_progress(label, "skip", "too_many_source_files")
                    continue

                # Validation (if we have a test_patch + caller didn't skip)
                fail_to_pass: list[str] = []
                pass_to_pass: list[str] = []
                validation_status = "no_test_patch"
                if test_patch.strip() and not self.options.skip_validation:
                    if sandbox is None:
                        sandbox = self._start_validation_sandbox()
                    from repo2rlenv.pipelines.pr_runtime_validate import validate_pr

                    targeted_cmds = targeted_test_cmds_for_pr(
                        normalize_test_cmds_for_runtime(self.bootstrap.test_cmds),
                        _files_in_patch(test_patch),
                    )
                    outcome = validate_pr(
                        sandbox=sandbox,
                        base_commit=parent_sha,
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
                        skip_reasons["no_fail_to_pass"] = skip_reasons.get("no_fail_to_pass", 0) + 1
                        self._emit_progress(label, "skip", outcome.reason or "no_fail_to_pass")
                        continue

                # Emit the Harbor task
                task = self._build_task(
                    vuln=vuln,
                    fix_sha=fix_sha,
                    parent_sha=parent_sha,
                    patch=patch,
                    test_patch=test_patch,
                    fail_to_pass=fail_to_pass,
                    pass_to_pass=pass_to_pass,
                    validation_status=validation_status,
                )
                write_harbor_task(task, out_dir)
                emitted += 1
                logger.info(
                    "emitted task %s (CVE=%s, F2P=%d, P2P=%d)",
                    task.name,
                    vuln.cve_id or vuln.id,
                    len(fail_to_pass),
                    len(pass_to_pass),
                )
                self._emit_progress(task.name, "emit")
        finally:
            if sandbox is not None:
                sandbox.cleanup()

        return PipelineResult(
            candidates=len(in_scope),
            emitted=emitted,
            skipped=sum(skip_reasons.values()),
            out_dir=out_dir,
            skip_reasons=skip_reasons,
        )

    # ----- sandbox -----------------------------------------------------------

    def _start_validation_sandbox(self):
        from repo2rlenv.bootstrap.docker import DockerSandbox

        marker = Path(tempfile.mkdtemp(prefix="r2e-cve-patches-"))
        (marker / ".keep").write_text("")
        return DockerSandbox.start(
            base_image=self.bootstrap.image_tag,
            repo_dir=marker,
            platform=self.input.bootstrap.platform,
        )

    # ----- task builder -------------------------------------------------------

    def _build_task(
        self,
        *,
        vuln: OSVVuln,
        fix_sha: str,
        parent_sha: str,
        patch: str,
        test_patch: str,
        fail_to_pass: list[str],
        pass_to_pass: list[str],
        validation_status: str,
    ) -> HarborTask:
        owner, name = self.input.repo.owner_name
        # Use the CVE id (or fallback to OSV id) for the task slug — stable + human-readable
        slug_id = (vuln.cve_id or vuln.id).replace("/", "_")
        task_id = f"{owner}__{name}-cve-{slug_id}"

        eval_script = build_eval_script(
            base_commit=parent_sha,
            test_patch=test_patch,
            test_cmds=targeted_test_cmds_for_pr(
                normalize_test_cmds_for_runtime(self.bootstrap.test_cmds),
                _files_in_patch(test_patch),
            ),
            language=self.bootstrap.language.value,
        )
        image_ref = (
            self.bootstrap.image_digest
            if self.bootstrap.pushed_to_registry
            else self.bootstrap.image_tag
        )
        dockerfile = build_environment_dockerfile(
            bootstrap_image=image_ref,
            base_commit=parent_sha,
        )

        repo2env = {
            "pipeline": "cve_patches",
            "pipeline_version": "0.7.0",
            "repo": f"{owner}/{name}",
            "ref": parent_sha,
            "reference": f"https://github.com/{owner}/{name}/commit/{fix_sha}",
            "source_access": self.input.repo.access,
            "built_at": datetime.now(UTC).isoformat(),
            **({"synthesis_llm": self.input.llm.qualified_name} if self.input.llm else {}),
            "reward_kinds": ["test_execution", "diff_similarity"],
            "cve_patches": {
                "cve_id": vuln.cve_id or "",
                "osv_id": vuln.id,
                "aliases": vuln.aliases,
                "cwe_ids": vuln.cwe_ids,
                "severity": vuln.severity_text,
                "published": vuln.published,
                "fix_commit": fix_sha,
                "parent_commit": parent_sha,
                "fail_to_pass": fail_to_pass,
                "pass_to_pass": pass_to_pass,
                "validation_status": validation_status,
                "bootstrap_image": self.bootstrap.image_digest,
            },
        }

        return HarborTask(
            name=task_id,
            org=self.input.output.org,
            description=vuln.summary or task_id,
            instruction=_build_instruction(vuln),
            oracle_diff=patch,
            repo2env=repo2env,
            difficulty="hard",  # CVE fixes are harder than vanilla bugs
            category="security",
            keywords=[name, "cve_patches", "security"],
            environment_dockerfile=dockerfile,
            test_script=eval_script,
        )
