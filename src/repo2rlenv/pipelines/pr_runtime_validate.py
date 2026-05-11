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
import re
from dataclasses import dataclass, field

from repo2rlenv.bootstrap.docker import DockerSandbox
from repo2rlenv.log_parsers import parse_pytest

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ValidationOutcome:
    status: str                            # verified | partial | failed | skipped
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)
    pre_log: str = ""                      # raw test output, pre-fix (truncated)
    post_log: str = ""                     # raw test output, post-fix
    reason: str = ""                       # populated on status != "verified"


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
    timeout: int = 600,
) -> ValidationOutcome:
    """Run the two-stage validation and return the resulting outcome.

    Re-uses a shared sandbox across PRs; the `git reset --hard` at the top of
    each stage script guarantees a clean working tree.
    """
    if not test_cmds:
        return ValidationOutcome(
            status="failed",
            reason="bootstrap did not record any test_cmds",
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
    pre_status = parse_pytest(_slice_test_output(pre_log))

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
    post_status = parse_pytest(_slice_test_output(post_log))

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
