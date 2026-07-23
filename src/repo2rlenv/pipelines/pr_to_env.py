"""Curated-URL PR → Harbor env conversion (RFC 0007).

The **import-shape** sibling of `pr_runtime`. Where `pr_runtime` mines a repo's
history and applies filters, `pr_to_env` consumes an explicit list of PR URLs
the user hands in — one URL, one Harbor task, or fail closed with a
per-URL reason.

Same task shape as `pr_runtime`, same graded F2P/P2P verifier, same
anti-contamination guards. The reused machinery is imported verbatim
from `pr_runtime.py` — no fork.

**HF_ML_Bench_v0 pipeline gates** (M3 layer, currently skeleton):
  1. Network-level egress firewall — replaces docker-compose extra_hosts
     (currently still uses egress_guard_compose; M2 swaps this)
  2. Bootstrap smoke — validated at bootstrap time
  3. F2P collect-only match — parametrization suffix expansion
  4. Reset decision table — git checkout vs rm -f based on base_commit content
  5. pyproject.toml sanitize — strip [tool.pytest] if [tool.pytest.ini_options]
  6. Cross-repo dep pinning — constraints.txt from merge-date `pip index versions`
  7. Salvage manifest — flags follow-up-commit collateral outside tests/
  8. F2P/P2P count floors — calibration = "low_signal" below min
  9. LF-normalized content_hash
 10. Instruction leak grep v2 — file basenames + dirs + SHA-8+ hex
 11. pytest.raises(match=...) flagged in DECISIONS.md
 12. Oracle-gate — reward=1.0 required or drop

Status: **experimental**. This is the first import-shape pipeline; the
gate set is still landing across M1-M4.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from repo2rlenv.auth import resolve_repo_token
from repo2rlenv.bootstrap.spec import BootstrapResult
from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.github import GitHubError, PullRequestSummary
from repo2rlenv.gitlab import GitLabError
from repo2rlenv.pipelines._env_guard import (
    egress_firewall_compose,
    egress_firewall_dockerfile_fragment,
)
from repo2rlenv.pipelines.base import PipelineResult

# Reuse verbatim from pr_runtime — the mining-shape sibling.
from repo2rlenv.pipelines.pr_runtime import (
    _build_instruction,
    _count_new_test_funcs,
    _diff_loc_changed,
    _difficulty_bucket,
    _files_in_patch,
    _is_non_bug_pr,
    _linked_issue_number,
    _runtime_aux_files,
    build_environment_dockerfile,
    build_eval_script,
    normalize_test_cmds_for_runtime,
    split_patch_and_test_patch,
    targeted_test_cmds_for_pr,
)
from repo2rlenv.provider import provider_for
from repo2rlenv.sources import Capability
from repo2rlenv.spec.input import GenerationInput, PipelineName
from repo2rlenv.spec.options import PrToEnvOptions

logger = logging.getLogger(__name__)

_PROVIDER_ERRORS = (GitHubError, GitLabError)

# ----------------------------------------------------------------------------
# URL parsing
# ----------------------------------------------------------------------------

# https://github.com/<owner>/<name>/pull/<n>  OR  gitlab.com/<owner>/<name>/-/merge_requests/<n>
_GH_PR_RE = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)/?$")
_GL_MR_RE = re.compile(r"^https?://gitlab\.com/([^/]+)/([^/]+)/-/merge_requests/(\d+)/?$")


class UrlParseError(ValueError):
    """Raised when a PR/MR URL doesn't match the expected shape."""


def parse_pr_url(url: str) -> tuple[str, str, str, int]:
    """Return (host, owner, repo, number) for a PR/MR URL.

    Supports github.com/*/pull/N and gitlab.com/*/-/merge_requests/N.
    """
    u = url.strip().rstrip("/")
    if m := _GH_PR_RE.match(u):
        return "github.com", m.group(1), m.group(2), int(m.group(3))
    if m := _GL_MR_RE.match(u):
        return "gitlab.com", m.group(1), m.group(2), int(m.group(3))
    raise UrlParseError(
        f"URL must be a github.com/*/pull/N or gitlab.com/*/-/merge_requests/N URL, got {url!r}"
    )


def read_urls_file(path: Path) -> list[str]:
    """Read URLs from a plain-text file, one per line. `#` comments allowed."""
    urls: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            urls.append(line)
    return urls


# ----------------------------------------------------------------------------
# Leak-strip v2 (gate #10) — layered ON TOP of pr_runtime._strip_info_leak.
# ----------------------------------------------------------------------------

# 8+ char bare hex tokens not caught by v1 (which only grabs the full 40-char
# form and refs adjacent to "cherry-pick"/"commit"). Matches short-SHA leaks
# embedded in prose without leading keyword.
_SHORT_SHA_RE = re.compile(r"\b[0-9a-f]{8,39}\b")

# pytest node-id prefix — reveals the exact test file/function that grades
# the fix. Full form: `tests/foo/test_bar.py::TestClass::test_method`.
_PYTEST_NODE_RE = re.compile(r"\btests?/[\w./-]+\.py(?:::[\w:-]+)?")


def _leak_grep_v2(
    instruction: str,
    source_files: list[str],
    test_files: list[str],
) -> tuple[str, list[str]]:
    """Extra leak-strip pass beyond `_strip_info_leak`.

    Returns `(cleaned_instruction, warnings)`. Warnings are non-fatal hits
    (basename / dirname mentions) — the caller decides whether to hard-drop
    or just annotate `DECISIONS.md`.

    Hard-strips (always removed):
      - Bare short-SHAs (8-39 hex chars) — git refs that reveal the fix commit.
      - Pytest node-id paths (`tests/foo/test_bar.py[::test_x]`) — grading target.

    Soft-flags (warnings only, not stripped — over-stripping legitimate prose
    like "look at `parser.py`" hurts instruction quality more than it helps):
      - File basenames of any touched source/test file.
      - Any component of a directory path touched by the diff (min length 4).
    """
    warnings: list[str] = []
    out = instruction

    # Hard-strip short-SHA hex.
    out = _SHORT_SHA_RE.sub("", out)
    # Hard-strip pytest node-ids.
    out = _PYTEST_NODE_RE.sub("", out)

    # Soft-flag basenames + dir components.
    for path in source_files + test_files:
        basename = path.rsplit("/", 1)[-1]
        if len(basename) >= 4 and re.search(rf"\b{re.escape(basename)}\b", out):
            warnings.append(f"basename '{basename}' appears in instruction")
        for part in path.split("/"):
            if (
                len(part) >= 4
                and part not in {"tests", "test"}
                and re.search(rf"\b{re.escape(part)}\b", out)
            ):
                warnings.append(f"dirname '{part}' appears in instruction")

    return out, warnings


# ----------------------------------------------------------------------------
# Dockerfile fragments — gate #5 (pyproject sanitize) + friends
# ----------------------------------------------------------------------------


def _pyproject_sanitize_snippet() -> str:
    """Dockerfile RUN step that strips `[tool.pytest]` from pyproject.toml
    when `[tool.pytest.ini_options]` also exists.

    Some repos (peft in particular) keep both sections for legacy reasons.
    Newer pytest treats the bare `[tool.pytest]` as invalid config and fails
    with exit-code 4 ("usage error") before running any test — this silently
    caused peft-2575 and peft-2952 to fail the oracle-gate on first pass.
    """
    return r"""
# Gate #5: sanitize pyproject.toml if both [tool.pytest] and
# [tool.pytest.ini_options] are present (pytest usage-error otherwise).
RUN python - <<'PY'
import pathlib, re, sys
p = pathlib.Path("/workspace/pyproject.toml")
if not p.exists():
    sys.exit(0)
text = p.read_text()
if "[tool.pytest]" in text and "[tool.pytest.ini_options]" in text:
    # Remove the bare [tool.pytest] section (keep [tool.pytest.ini_options]).
    cleaned = re.sub(
        r"^\[tool\.pytest\](?![\.\w]).*?(?=^\[|\Z)",
        "",
        text,
        count=1,
        flags=re.MULTILINE | re.DOTALL,
    )
    p.write_text(cleaned)
    print("sanitized pyproject.toml: removed bare [tool.pytest]", file=sys.stderr)
PY
"""


# ----------------------------------------------------------------------------
# Pipeline
# ----------------------------------------------------------------------------


class PrToEnvPipeline:
    """Curated-URL PR → Harbor env. Implements the `Pipeline` Protocol.

    The user must have already run `repo2rlenv bootstrap --repo <repo>` for
    the repo whose PRs they're importing — the bootstrap image is required
    (mirrors pr_runtime). All URLs in a single call must be from the same
    repo, matching `input.repo`.

    Cross-repo multi-URL calls are out of scope for this iteration; wrap the
    CLI in a script if you need it.
    """

    name: ClassVar[PipelineName] = PipelineName.PR_TO_ENV
    required_capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.PULL_REQUESTS})
    requires_bootstrap: ClassVar[bool] = True
    experimental: ClassVar[bool] = True  # still landing gates M1-M4

    def __init__(
        self,
        input: GenerationInput,
        options: PrToEnvOptions,
        bootstrap: BootstrapResult | None = None,
    ):
        if bootstrap is None:
            raise RuntimeError(
                "pr_to_env requires a BootstrapResult (set requires_bootstrap=True "
                "and let cmd_generate trigger it, or pass one explicitly)"
            )
        if options.url is None and options.urls_file is None:
            raise ValueError("pr_to_env requires exactly one of --pipeline-opt url= or urls_file=")
        if options.url is not None and options.urls_file is not None:
            raise ValueError("pr_to_env: pass exactly one of url= or urls_file=, not both")

        self.input = input
        self.options = options
        self.bootstrap = bootstrap
        self._progress_cb = None
        self._token: str | None = None

    def set_progress_callback(self, cb) -> None:
        self._progress_cb = cb

    def _emit_progress(self, name: str, outcome: str, reason: str = "") -> None:
        if self._progress_cb is not None:
            try:
                self._progress_cb(name=name, outcome=outcome, reason=reason)
            except Exception as exc:
                logger.debug("progress callback failed: %s", exc)

    # ---- URL resolution --------------------------------------------------

    def _collect_urls(self) -> list[str]:
        if self.options.urls_file is not None:
            urls = read_urls_file(self.options.urls_file)
        else:
            assert self.options.url is not None  # validated in __init__
            urls = [self.options.url]
        if not urls:
            raise ValueError("pr_to_env: URL list is empty")
        return urls

    def _validate_single_repo(self, urls: list[str]) -> tuple[str, str, str]:
        """Ensure every URL points at the same (host, owner, repo). Return the triple.

        Also verify it matches `input.repo` (the anchor the bootstrap was built for).
        """
        parsed = [parse_pr_url(u) for u in urls]
        hosts = {p[0] for p in parsed}
        owner_repos = {(p[1], p[2]) for p in parsed}
        if len(hosts) > 1 or len(owner_repos) > 1:
            raise ValueError(
                "pr_to_env: all URLs must be from the same host+repo in a single call. "
                f"Got hosts={hosts} owner_repos={owner_repos}. "
                "Wrap the CLI in a script to iterate across repos."
            )
        host = next(iter(hosts))
        owner, repo = next(iter(owner_repos))

        # Cross-check against input.repo (the bootstrap anchor)
        expected_owner, expected_name = self.input.repo.owner_name
        if (owner, repo) != (expected_owner, expected_name):
            raise ValueError(
                f"pr_to_env: --repo is {expected_owner}/{expected_name} but URLs point at "
                f"{owner}/{repo}. They must match (bootstrap image is per-repo)."
            )
        return host, owner, repo

    # ---- Run loop --------------------------------------------------------

    def run(self, out_dir: Path) -> PipelineResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        urls = self._collect_urls()
        _host, owner, name = self._validate_single_repo(urls)

        token = resolve_repo_token(self.input.repo, self.input.auth)
        self._token = token
        provider = provider_for(self.input.repo)

        skip_reasons: dict[str, int] = {}
        emitted = 0
        candidates = len(urls)
        sandbox = None

        try:
            for url in urls:
                _, _, _, pr_number = parse_pr_url(url)
                pr_label = f"{owner}/{name}#{pr_number}"

                # Fetch PR metadata
                try:
                    pr = provider.fetch_pr(owner, name, pr_number, token=token)
                except _PROVIDER_ERRORS as exc:
                    reason = "pr_fetch_failed"
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    self._emit_progress(pr_label, "error", f"{reason}: {exc}")
                    if self.options.strict:
                        raise
                    continue

                # Non-bug filter (reverts, cherry-picks, release chores)
                if _is_non_bug_pr(pr.title):
                    reason = "non_bug_pr"
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    self._emit_progress(pr_label, "skip", reason)
                    if self.options.strict:
                        raise ValueError(f"URL {url!r} is a non-bug PR (title: {pr.title!r})")
                    continue

                # Fetch diff
                try:
                    diff = provider.fetch_pr_diff(owner, name, pr_number, token=token)
                except _PROVIDER_ERRORS as exc:
                    reason = "diff_fetch_failed"
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    self._emit_progress(pr_label, "error", f"{reason}: {exc}")
                    if self.options.strict:
                        raise
                    continue

                # Split source vs test
                patch, test_patch = split_patch_and_test_patch(diff)
                if not patch.strip():
                    reason = "empty_source_patch"
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    self._emit_progress(pr_label, "skip", reason)
                    if self.options.strict:
                        raise ValueError(f"URL {url!r}: source patch is empty")
                    continue
                if not test_patch.strip():
                    reason = "no_test_patch"
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    self._emit_progress(pr_label, "skip", reason)
                    if self.options.strict:
                        raise ValueError(f"URL {url!r}: PR added no test files (no F2P signal)")
                    continue

                # Structural gate: at least one new test function
                if self.options.require_new_test_funcs:
                    n_new = _count_new_test_funcs(test_patch)
                    if n_new == 0:
                        reason = "no_new_test_funcs"
                        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                        self._emit_progress(pr_label, "skip", reason)
                        if self.options.strict:
                            raise ValueError(
                                f"URL {url!r}: test_patch modifies existing tests only "
                                "(no +def test_ hunks)"
                            )
                        continue

                # Two-stage F2P/P2P validation in the sandbox
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
                        base_commit=pr.base_sha,
                        patch=patch,
                        test_patch=test_patch,
                        test_cmds=targeted_cmds,
                        language=self.bootstrap.language.value,
                        timeout=self.options.validation_timeout_sec,
                    )
                    fail_to_pass = outcome.fail_to_pass
                    pass_to_pass = outcome.pass_to_pass
                    validation_status = outcome.status

                # Count floors (M3 gate #10)
                if len(fail_to_pass) < self.options.min_f2p:
                    reason = "f2p_below_floor"
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    self._emit_progress(
                        pr_label,
                        "skip",
                        f"{reason} ({len(fail_to_pass)} < {self.options.min_f2p})",
                    )
                    if self.options.hard_drop_low_signal and self.options.strict:
                        raise ValueError(
                            f"URL {url!r}: F2P count {len(fail_to_pass)} < {self.options.min_f2p}"
                        )
                    if self.options.hard_drop_low_signal:
                        continue

                # ---- Emit task ---------------------------------------------
                task = self._build_task(
                    pr=pr,
                    patch=patch,
                    test_patch=test_patch,
                    fail_to_pass=fail_to_pass,
                    pass_to_pass=pass_to_pass,
                    validation_status=validation_status,
                )
                slug = f"{owner}__{name}-{pr_number}"
                write_harbor_task(task, out_dir / slug)

                # Oracle-gate (M3 gate #14) — currently deferred to a post-emit
                # script. Full integration lands in M3.
                if self.options.oracle_gate:
                    logger.warning(
                        "oracle-gate not yet integrated in-pipeline; run "
                        "`plans/scripts/harbor_eval_env.sh %s` post-emit and "
                        "drop the env if reward != 1.0.",
                        slug,
                    )

                # Oracle-gate (M3 gate #14) — run `harbor run -a oracle` and drop
                # the env unless reward == 1.0. Skip if user disabled it or the
                # harbor CLI isn't reachable (soft-warn only).
                if self.options.oracle_gate:
                    reward = self._run_oracle_gate(
                        task_dir=out_dir / slug,
                        timeout_sec=self.options.oracle_timeout_sec,
                    )
                    if reward is None:
                        logger.warning(
                            "oracle-gate skipped for %s (harbor CLI not reachable)", slug
                        )
                        self._append_ledger(
                            out_dir,
                            slug,
                            pr.url,
                            "oracle_skipped",
                            None,
                            len(fail_to_pass),
                            len(pass_to_pass),
                        )
                    elif reward < 1.0:
                        # Drop the env — oracle couldn't earn reward=1.0.
                        shutil.rmtree(out_dir / slug, ignore_errors=True)
                        reason = "oracle_below_1"
                        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                        self._emit_progress(pr_label, "skip", f"{reason} (reward={reward})")
                        self._append_ledger(
                            out_dir,
                            slug,
                            pr.url,
                            "dropped",
                            reward,
                            len(fail_to_pass),
                            len(pass_to_pass),
                        )
                        if self.options.strict:
                            raise ValueError(f"URL {url!r}: oracle reward={reward} < 1.0")
                        continue
                    else:
                        self._append_ledger(
                            out_dir,
                            slug,
                            pr.url,
                            "keeper",
                            reward,
                            len(fail_to_pass),
                            len(pass_to_pass),
                        )

                emitted += 1
                self._emit_progress(pr_label, "emit", f"f2p={len(fail_to_pass)}")
        finally:
            if sandbox is not None:
                try:
                    sandbox.stop()
                except Exception:
                    logger.debug("sandbox stop failed", exc_info=True)

        return PipelineResult(
            candidates=candidates,
            emitted=emitted,
            skipped=sum(skip_reasons.values()),
            out_dir=out_dir,
            skip_reasons=skip_reasons,
        )

    # ---- Helpers ---------------------------------------------------------

    def _run_oracle_gate(self, task_dir: Path, timeout_sec: int) -> float | None:
        """Run `harbor run -a oracle` on the emitted task and return the reward.

        Returns None if the harbor CLI isn't on PATH or exits abnormally — the
        caller treats this as a soft-skip (env is kept, ledger records
        oracle_skipped). Returns a float in [0.0, 1.0] on a real run.
        """
        harbor = shutil.which("harbor")
        if harbor is None:
            return None
        try:
            proc = subprocess.run(
                [harbor, "run", "-a", "oracle", "--task-dir", str(task_dir)],
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("oracle-gate subprocess failed for %s: %s", task_dir.name, exc)
            return None
        # Harbor writes reward to reward.txt in the task's verifier dir. Fall back
        # to parsing stdout for `reward=<float>` if not found.
        combined = f"{proc.stdout}\n{proc.stderr}"
        m = re.search(r"reward\s*[=:]\s*([0-9.]+)", combined)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
        return None

    def _append_ledger(
        self,
        out_dir: Path,
        slug: str,
        pr_url: str,
        status: str,
        reward: float | None,
        f2p_count: int,
        p2p_count: int,
    ) -> None:
        """Append one line to `keepers.jsonl` at the out_dir root."""
        ledger_path = out_dir / "keepers.jsonl"
        entry = {
            "slug": slug,
            "pr_url": pr_url,
            "status": status,
            "reward": reward,
            "f2p_count": f2p_count,
            "p2p_count": p2p_count,
            "timestamp": datetime.now(UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        with ledger_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(entry) + "\n")

    def _start_validation_sandbox(self):
        """Create a sandbox container from the bootstrap image."""
        from repo2rlenv.bootstrap.docker import DockerSandbox

        sandbox = DockerSandbox(
            image=self.bootstrap.image_digest or self.bootstrap.image_tag,
            language=self.bootstrap.language,
        )
        sandbox.start()
        return sandbox

    def _build_task(
        self,
        *,
        pr: PullRequestSummary,
        patch: str,
        test_patch: str,
        fail_to_pass: list[str],
        pass_to_pass: list[str],
        validation_status: str,
    ) -> HarborTask:
        """Assemble a HarborTask from the split diff + validation output."""
        owner, name = self.input.repo.owner_name
        slug = f"{owner}__{name}-{pr.number}"

        # Instruction (leak-free per pr_runtime._build_instruction). Linked-issue
        # text is looked up if the PR body cites one.
        linked_issue = _linked_issue_number(pr.body or "")
        issue_body = None
        if linked_issue is not None:
            try:
                provider = provider_for(self.input.repo)
                issue_body = (
                    provider.fetch_issue_body(owner, name, linked_issue, token=self._token)
                    if hasattr(provider, "fetch_issue_body")
                    else None
                )
            except Exception:
                issue_body = None

        instruction = _build_instruction(
            pr_title=pr.title,
            pr_body=pr.body or "",
            linked_issue_body=issue_body,
        )

        # Leak-strip v2 (gate #10): remove short-SHAs and pytest node-ids that
        # sneak past the v1 patterns; soft-warn on basename/dirname hits.
        source_files = _files_in_patch(patch)
        test_files = _files_in_patch(test_patch)
        instruction, leak_warnings = _leak_grep_v2(instruction, source_files, test_files)
        if leak_warnings:
            logger.info("leak-v2 soft-warn for %s: %s", slug, "; ".join(leak_warnings[:5]))

        # Environment Dockerfile + egress-guard docker-compose (v2 network-level).
        # The v2 guard installs iptables in the image + a default-deny OUTPUT
        # policy inside the container. Closes the raw.githubusercontent.com
        # leak that opus/codex exploited on HF_ML_Bench_v0.
        image_ref = self.bootstrap.image_digest or self.bootstrap.image_tag
        env_dockerfile = (
            build_environment_dockerfile(
                bootstrap_image=image_ref,
                base_commit=pr.base_sha,
            )
            + "\n"
            + _pyproject_sanitize_snippet()
            + "\n"
            + egress_firewall_dockerfile_fragment()
        )
        env_compose = egress_firewall_compose()

        # tests/test.sh (eval script) + aux files
        targeted_cmds = targeted_test_cmds_for_pr(
            normalize_test_cmds_for_runtime(self.bootstrap.test_cmds),
            _files_in_patch(test_patch),
        )
        eval_script = build_eval_script(
            base_commit=pr.base_sha,
            test_patch=test_patch,
            test_cmds=targeted_cmds,
            language=self.bootstrap.language.value,
            fail_to_pass=fail_to_pass,
            pass_to_pass=pass_to_pass,
        )
        aux_files = _runtime_aux_files(fail_to_pass, pass_to_pass)

        # Difficulty + provenance (source_files/test_files already computed
        # above for leak-v2).
        loc_changed = _diff_loc_changed(patch) + _diff_loc_changed(test_patch)
        difficulty = _difficulty_bucket(len(fail_to_pass), loc_changed)

        now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        return HarborTask(
            name=f"{owner}/{slug}",
            description=pr.title,
            category="code-modification",
            keywords=[name, "pr_to_env", "sandbox-verified"],
            difficulty=difficulty,
            instruction=instruction,
            oracle_diff=patch,
            solve_cmd="git apply /workspace/solution/patch.diff",
            eval_script=eval_script,
            env_dockerfile=env_dockerfile,
            env_compose=env_compose,
            aux_files=aux_files,
            provenance={
                "pipeline": "pr_to_env",
                "pipeline_version": "0.9.0",
                "repo": f"{owner}/{name}",
                "ref": pr.base_sha,
                "reference": pr.url,
                "source_access": self.input.repo.access,
                "built_at": now,
                "reward_kinds": ["test_execution", "diff_similarity"],
                "pr_to_env": {
                    "pr_url": pr.url,
                    "source_url": pr.url,
                    "pr_merged_at": pr.merged_at,
                    "base_commit": pr.base_sha,
                    "fail_to_pass": fail_to_pass,
                    "pass_to_pass": pass_to_pass,
                    "validation_status": validation_status,
                    "bootstrap_image": image_ref,
                    "reward_mode": "graded",
                },
                "reward_calibration": {
                    "f2p_count": len(fail_to_pass),
                    "p2p_count": len(pass_to_pass),
                    "source_files": source_files,
                    "loc_changed": loc_changed,
                    "difficulty": difficulty,
                    "calibration": (
                        "low_signal"
                        if len(fail_to_pass) < self.options.min_f2p
                        or len(pass_to_pass) < self.options.min_p2p
                        else "ok"
                    ),
                },
            },
        )
