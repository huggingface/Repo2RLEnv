"""Validate a candidate PR inside the bootstrap container.

Computes the FAIL_TO_PASS + PASS_TO_PASS sets that SWE-bench-style oracles
need. Workflow inside one container:

  1. Reset working tree to base_commit (`git reset --hard`)
  2. Apply test_patch only           → run tests → pre_status (per-test)
  3. Reset, then apply patch + test_patch → run tests → post_status
  4. F2P = tests that FAILED in (2) and PASS in (3)
     P2P = tests that PASSED in both (2) and (3)

We mirror SWE-bench's harness/test_spec/utils.py:make_eval_script_list_common
for the per-stage script, and harness/grading.py:get_logs_eval for the
status-extraction direction. Implementation is independent (no swebench
import); see references/SWE-bench/ for the reference code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from repo2rlenv.bootstrap.docker import DockerSandbox
from repo2rlenv.log_parsers import parse_logs

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ValidationOutcome:
    status: str  # verified | partial | failed | skipped
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)
    pre_log: str = ""  # raw test output, pre-fix (truncated)
    post_log: str = ""  # raw test output, post-fix
    reason: str = ""  # populated on status != "verified"


_HEREDOC = "EOF_R2E_VALIDATE"


def _heredoc_apply(patch: str) -> str:
    return f"git apply --verbose --reject - <<'{_HEREDOC}'\n{patch}\n{_HEREDOC}"


def _build_stage_script(
    base_commit: str,
    *,
    apply_patch: str | None,
    apply_test_patch: str | None,
    test_cmds: list[str],
) -> str:
    """Build the shell script for one validation stage (pre-fix or post-fix).

    Always resets to base_commit first, then applies whichever patches the
    caller supplies (None ⇒ skip), then runs the test commands wrapped in
    START/END markers so the parser knows where output starts.

    Assumes the caller has already ensured `base_commit` is fetchable in the
    container's git object database (see `_fetch_base_commit`). The bootstrap
    image only ships the shallow-clone HEAD, so historical PR base commits
    aren't present until we fetch them explicitly.
    """
    parts: list[str] = [
        "set -uxo pipefail",
        "cd /workspace",
        "git config --global --add safe.directory /workspace",
        f"git reset --hard {base_commit}",
        "git clean -fdx -e .venv -e venv -e __pycache__ || true",
    ]
    if apply_patch and apply_patch.strip():
        parts.append(_heredoc_apply(apply_patch))
    if apply_test_patch and apply_test_patch.strip():
        parts.append(_heredoc_apply(apply_test_patch))
    parts.append(": 'START_TEST_OUTPUT'")
    parts.append(" && ".join(test_cmds) if test_cmds else "echo 'no test_cmds'")
    parts.append(": 'END_TEST_OUTPUT'")
    return "\n".join(parts)


_GIT_DEFENSIVE_INSTALL = (
    "command -v git >/dev/null 2>&1 || "
    "(apt-get update >/dev/null 2>&1 && "
    "apt-get install -y --no-install-recommends git >/dev/null 2>&1 && "
    "rm -rf /var/lib/apt/lists/*) || "
    "apk add --no-cache git >/dev/null 2>&1 || true"
)


def _ensure_git(sandbox: DockerSandbox) -> bool:
    """Install git in the sandbox if missing + mark the repo as safe.directory.

    Idempotent — no-op when git is already on PATH. Returns True if git is
    available after the call. Tries apt-get (Debian/Ubuntu) then apk (Alpine).
    Always marks /workspace as a safe directory afterward because Docker copies
    the repo in as root-owned, which triggers git's `detected dubious ownership`
    refusal otherwise.
    """
    check = sandbox.exec("command -v git >/dev/null 2>&1 && echo OK", timeout=10)
    if not (check.ok and "OK" in check.stdout):
        sandbox.exec(_GIT_DEFENSIVE_INSTALL, timeout=120)
        re = sandbox.exec("command -v git >/dev/null 2>&1 && echo OK", timeout=10)
        if not (re.ok and "OK" in re.stdout):
            return False
    sandbox.exec("git config --global --add safe.directory /workspace", timeout=10)
    return True


def _fetch_base_commit(sandbox: DockerSandbox, base_commit: str, *, timeout: int = 120) -> bool:
    """Make `base_commit` available in the container's git object db.

    Bootstrap shallow-clones at depth=1, so only the bootstrap-time HEAD is
    present. Without this fetch, `git reset --hard <historical_sha>` would
    fail silently or reset against the wrong tree, masking validation bugs.

    Strategy:
      0. Ensure git is installed (Python-slim bootstrap images often skip it).
      1. If the commit already exists locally (e.g. it IS bootstrap HEAD or
         we fetched it on a prior PR in this sandbox), skip — fast path.
      2. Else `git fetch --depth 1 origin <sha>` to pull just that one commit.
         Falls back to deepening the shallow clone if the server refuses
         a by-sha fetch.

    Returns True if base_commit is now reachable, False otherwise.
    """
    if not _ensure_git(sandbox):
        logger.warning("validate_pr: could not install git in sandbox; skipping fetch")
        return False
    # Fast path: already present
    have = sandbox.exec(
        f"git -C /workspace cat-file -e {base_commit} 2>/dev/null && echo OK",
        timeout=15,
    )
    if have.ok and "OK" in have.stdout:
        return True

    # Try direct by-sha fetch (works on GitHub since 2017's uploadpack.allowAnySHA1InWant)
    r = sandbox.exec(
        f"cd /workspace && git fetch --depth 1 origin {base_commit}",
        timeout=timeout,
    )
    if r.ok:
        return True
    logger.warning(
        "validate_pr: direct fetch of %s failed (%s); deepening clone",
        base_commit[:12],
        r.stderr.strip()[:200] if r.stderr else "",
    )
    # Fallback: unshallow. Slower but always works.
    r = sandbox.exec(
        "cd /workspace && git fetch --unshallow origin || git fetch --depth 1000 origin",
        timeout=timeout * 2,
    )
    if not r.ok:
        return False
    # Re-check
    have = sandbox.exec(
        f"git -C /workspace cat-file -e {base_commit} 2>/dev/null && echo OK",
        timeout=15,
    )
    return have.ok and "OK" in have.stdout


def _slice_test_output(output: str) -> str:
    """Trim to just the test-runner section between the START/END markers."""
    start = output.find("START_TEST_OUTPUT")
    end = output.find("END_TEST_OUTPUT")
    if start == -1:
        return output
    chunk = output[start:end] if end > start else output[start:]
    # Drop the marker line itself
    nl = chunk.find("\n")
    return chunk[nl + 1 :] if nl != -1 else chunk


def validate_pr(
    *,
    sandbox: DockerSandbox,
    base_commit: str,
    patch: str,
    test_patch: str,
    test_cmds: list[str],
    language: str | None = None,
    timeout: int = 600,
) -> ValidationOutcome:
    """Run the two-stage validation and return the resulting outcome.

    Re-uses a shared sandbox across PRs; the `git reset --hard` at the top of
    each stage script guarantees a clean working tree.

    `language` is the LanguageHint value (e.g. "python", "go") used as a
    fallback when test_cmds doesn't name a known runner. Almost always the
    runner is inferred from test_cmds — language is just a safety net.
    """
    if not test_cmds:
        return ValidationOutcome(
            status="failed",
            reason="bootstrap did not record any test_cmds",
        )

    # Ensure the base commit is in the container's object db before any
    # `git reset --hard <sha>`. Bootstrap shallow-clones at depth=1, so
    # historical PR base commits aren't present by default.
    if not _fetch_base_commit(sandbox, base_commit, timeout=timeout):
        return ValidationOutcome(
            status="failed",
            reason=f"could not fetch base_commit {base_commit[:12]} in sandbox",
        )

    # Stage 1: pre-fix (apply test_patch only) — captures the "buggy" baseline.
    pre_script = _build_stage_script(
        base_commit,
        apply_patch=None,
        apply_test_patch=test_patch,
        test_cmds=test_cmds,
    )
    logger.info("validate_pr: running pre-fix stage at %s", base_commit[:12])
    pre = sandbox.exec(pre_script, timeout=timeout)
    pre_log = pre.truncated(max_chars=20_000)
    pre_status = parse_logs(test_cmds, _slice_test_output(pre_log), language=language)

    # If the test_patch itself failed to apply, no point continuing.
    if "error: patch failed" in pre_log.lower() or "patch does not apply" in pre_log.lower():
        return ValidationOutcome(
            status="failed",
            reason="test_patch failed to apply at base_commit",
            pre_log=pre_log,
        )

    # Stage 2: post-fix (apply both patch and test_patch).
    post_script = _build_stage_script(
        base_commit,
        apply_patch=patch,
        apply_test_patch=test_patch,
        test_cmds=test_cmds,
    )
    logger.info("validate_pr: running post-fix stage")
    post = sandbox.exec(post_script, timeout=timeout)
    post_log = post.truncated(max_chars=20_000)
    post_status = parse_logs(test_cmds, _slice_test_output(post_log), language=language)

    if "error: patch failed" in post_log.lower() or "patch does not apply" in post_log.lower():
        return ValidationOutcome(
            status="failed",
            reason="gold patch failed to apply at base_commit",
            pre_log=pre_log,
            post_log=post_log,
        )

    # Compute F2P / P2P. Both sets are over the union of test names seen.
    fail_to_pass: list[str] = []
    pass_to_pass: list[str] = []
    for tname, pre_st in pre_status.items():
        post_st = post_status.get(tname)
        if pre_st == "FAILED" and post_st == "PASSED":
            fail_to_pass.append(tname)
        elif pre_st == "PASSED" and post_st == "PASSED":
            pass_to_pass.append(tname)

    if not fail_to_pass:
        return ValidationOutcome(
            status="failed",
            reason="no fail-to-pass tests after validation",
            fail_to_pass=fail_to_pass,
            pass_to_pass=pass_to_pass,
            pre_log=pre_log,
            post_log=post_log,
        )

    return ValidationOutcome(
        status="verified",
        fail_to_pass=sorted(fail_to_pass),
        pass_to_pass=sorted(pass_to_pass),
        pre_log=pre_log,
        post_log=post_log,
    )
