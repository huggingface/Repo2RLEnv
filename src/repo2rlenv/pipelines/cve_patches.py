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
import os
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
    fetch_file_at_ref,
)
from repo2rlenv.llm import complete
from repo2rlenv.osv import OSVError, OSVVuln, guess_ecosystem, query_vulns, severity_at_least
from repo2rlenv.pipelines.base import PipelineResult
from repo2rlenv.pipelines.code_instruct import new_file_hunk
from repo2rlenv.pipelines.pr_runtime import (
    _files_in_patch,
    build_environment_dockerfile,
    build_eval_script,
    normalize_test_cmds_for_runtime,
    split_patch_and_test_patch,
    targeted_test_cmds_for_pr,
)
from repo2rlenv.sources import Capability
from repo2rlenv.spec.input import GenerationInput, PipelineName
from repo2rlenv.spec.options import CVEPatchesOptions

logger = logging.getLogger(__name__)

# Synthesis prompt: write the regression test the CVE fix never shipped. The
# test must FAIL on the pre-fix (vulnerable) code and PASS once the fix is
# applied — that's what turns a CVE into a verifiable F2P oracle.
_POC_SYSTEM = """You write a pytest regression test that proves a security fix.

You are given a CVE description and the unified diff of the commit that FIXED it.
Write ONE pytest test file that exercises the vulnerable behavior such that:
- on the code BEFORE the fix it FAILS (the vulnerability/incorrect behavior is present),
- on the code AFTER the fix it PASSES.

STRICT RULES:
- Output ONLY the Python test file content — no prose, no markdown fences.
- Use plain `def test_*():` functions with `assert` (no unittest.TestCase, no network, no sleep/timing).
- Import from the library under test by its real package name; build inputs as literals.
- The test must be deterministic and self-contained. Target the SPECIFIC behavior the
  diff changes (e.g. the now-rejected input, the corrected parse result), not unrelated APIs.
- Do NOT reference the fix, commit SHAs, or that a patch exists."""


def _poc_user_prompt(vuln: OSVVuln, patch: str, vuln_code: str = "") -> str:
    desc = (vuln.summary or "") + "\n\n" + (vuln.details or "")
    parts = [f"CVE: {vuln.cve_id or vuln.id}\n\nDescription:\n{desc.strip()[:3000]}\n"]
    if vuln_code:
        parts.append(
            "\nVulnerable source BEFORE the fix (use this for the real import paths "
            f"and how to call the affected code):\n```python\n{vuln_code[:9000]}\n```\n"
        )
    parts.append(f"\nThe fix (what changed):\n```diff\n{patch[:6000]}\n```\n")
    parts.append("Write the regression test now (file content only).")
    return "\n".join(parts)


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
    required_capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.COMMIT_API})
    requires_bootstrap: ClassVar[bool] = True
    experimental: ClassVar[bool] = True

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
        self._llm_cost_usd = 0.0

    def _synthesize_poc_test(
        self, vuln: OSVVuln, patch: str, owner: str, name: str, base_sha: str, token: str | None
    ) -> str | None:
        """LLM-write the regression test the CVE fix omitted. Returns file content or None.

        Feeds the LLM the *full vulnerable source* of the changed files (not just
        the diff) so it knows the real import paths + how to call the affected
        code — the diff alone is rarely enough to write a runnable test.
        """
        if self.input.llm is None:
            return None
        vuln_code = ""
        for f in _files_in_patch(patch)[:2]:
            content = fetch_file_at_ref(owner, name, f, base_sha, token=token)
            if content:
                vuln_code += f"# ===== {f} =====\n{content}\n\n"
        try:
            resp = complete(
                self.input.llm,
                system=_POC_SYSTEM,
                user=_poc_user_prompt(vuln, patch, vuln_code),
                max_tokens=self.options.max_llm_tokens,
                temperature=self.options.llm_temperature,
            )
        except Exception as exc:
            logger.warning("cve_patches PoC synthesis failed for %s: %s", vuln.id, exc)
            return None
        self._llm_cost_usd += resp.cost_usd
        code = (resp.content or "").strip()
        # strip an accidental ```python fence if the model added one
        if code.startswith("```"):
            code = code.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if os.environ.get("R2E_POC_DEBUG") and code:  # debug aid: inspect generated tests
            dbg = Path("/tmp/poc_debug")
            dbg.mkdir(exist_ok=True)
            (dbg / f"{(vuln.cve_id or vuln.id).replace('/', '_')}.py").write_text(code)
        return code if ("def test" in code and len(code) > 40) else None

    @staticmethod
    def _poc_test_patch(test_code: str, slug: str) -> str:
        """Wrap synthesized test code as a new-file `git apply` diff under tests/."""
        path = f"tests/test_cve_{slug.lower().replace('-', '_')}.py"
        return new_file_hunk(test_code if test_code.endswith("\n") else test_code + "\n", path)

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

                # Build the F2P oracle. Two sources of a fail→pass test:
                #   1. a real test_patch shipped in the fix commit (rare for CVEs)
                #   2. an LLM-synthesized PoC test (default) — must FAIL pre-fix,
                #      PASS post-fix, validated below. This is what makes a CVE
                #      whose fix shipped no test into a verifiable, non-gameable env.
                fail_to_pass: list[str] = []
                pass_to_pass: list[str] = []
                validation_status = "no_test_patch"
                effective_test_patch = test_patch
                lang = self.bootstrap.language.value

                def _validate(tp: str, base: str, src_patch: str, language: str):
                    nonlocal sandbox
                    if sandbox is None:
                        sandbox = self._start_validation_sandbox()
                    from repo2rlenv.pipelines.pr_runtime_validate import validate_pr

                    return validate_pr(
                        sandbox=sandbox,
                        base_commit=base,
                        patch=src_patch,
                        test_patch=tp,
                        test_cmds=targeted_test_cmds_for_pr(
                            normalize_test_cmds_for_runtime(self.bootstrap.test_cmds),
                            _files_in_patch(tp),
                        ),
                        language=language,
                        timeout=self.options.validation_timeout_sec,
                    )

                if self.options.skip_validation:
                    pass
                elif test_patch.strip():
                    outcome = _validate(test_patch, parent_sha, patch, lang)
                    fail_to_pass, pass_to_pass, validation_status = (
                        outcome.fail_to_pass,
                        outcome.pass_to_pass,
                        outcome.status,
                    )
                elif self.options.synthesize_poc_test and lang == "python" and self.input.llm:
                    if sandbox is None:
                        sandbox = self._start_validation_sandbox()
                    candidates: list[str] = []
                    if self.options.poc_agent:
                        # Agentic: an LLM with shell access in the vulnerable sandbox
                        # explores the repo, writes the test, runs pytest, iterates —
                        # so the test imports correctly and reproduces the CVE.
                        from repo2rlenv.pipelines._poc_agent import synthesize_poc_agentic

                        vuln_desc = (vuln.summary or "") + "\n\n" + (vuln.details or "")
                        result, c = synthesize_poc_agentic(
                            sandbox,
                            parent_sha=parent_sha,
                            vuln_desc=vuln_desc,
                            fix_diff=patch,
                            llm=self.input.llm,
                            max_spend_usd=self.options.poc_agent_max_spend_usd,
                        )
                        self._llm_cost_usd += c
                        if result:
                            code = (
                                result.test_code
                                if result.test_code.endswith("\n")
                                else result.test_code + "\n"
                            )
                            candidates.append(new_file_hunk(code, result.test_path.lstrip("/")))
                    else:
                        # One-shot prompt synthesis (fallback), retried.
                        for _ in range(max(1, self.options.poc_max_attempts)):
                            code = self._synthesize_poc_test(
                                vuln, patch, owner, name, parent_sha, token
                            )
                            if code:
                                candidates.append(
                                    self._poc_test_patch(code, vuln.cve_id or vuln.id)
                                )

                    for cand in candidates:
                        outcome = _validate(cand, parent_sha, patch, lang)
                        if len(outcome.fail_to_pass) >= self.options.min_fail_to_pass:
                            effective_test_patch = cand
                            fail_to_pass, pass_to_pass = outcome.fail_to_pass, outcome.pass_to_pass
                            validation_status = "poc_synthesized"
                            break

                # Require a real oracle: drop dead (0-reward) envs.
                if (
                    self.options.require_fail_to_pass
                    and len(fail_to_pass) < self.options.min_fail_to_pass
                ):
                    skip_reasons["no_verifiable_oracle"] = (
                        skip_reasons.get("no_verifiable_oracle", 0) + 1
                    )
                    self._emit_progress(label, "skip", "no_verifiable_oracle")
                    continue

                # Cap the P2P regression set (bounds flaky-reward + runtime).
                cap = self.options.max_pass_to_pass
                if cap and len(pass_to_pass) > cap:
                    pass_to_pass = pass_to_pass[:cap]

                # Emit the Harbor task
                task = self._build_task(
                    vuln=vuln,
                    fix_sha=fix_sha,
                    parent_sha=parent_sha,
                    patch=patch,
                    test_patch=effective_test_patch,
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
            "pipeline_version": "0.8.5",
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
                "poc_synthesized": validation_status == "poc_synthesized",
                "llm_cost_usd": round(self._llm_cost_usd, 6),
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
