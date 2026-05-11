"""Diff-similarity reward.

Computes a [0, 1] score by comparing a predicted unified diff against an oracle
diff using sequence similarity. Verifiable: identical diffs ⇒ 1.0; unrelated
diffs ⇒ near 0. Uses only stdlib (`difflib`).

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
The reward concept (sequence-similarity over normalized diffs) is inspired by:

  SWE-RL: Advancing LLM Reasoning via Reinforcement Learning on Open Software
  Evolution (Wei et al., NeurIPS '25, arXiv:2502.18449)
  https://github.com/facebookresearch/swe-rl
  Reward library license: CC BY-NC 4.0 (non-commercial)

This file is an INDEPENDENT REIMPLEMENTATION. No code is copied from SWE-RL —
we use only Python's standard library (`difflib.SequenceMatcher`). The CC
BY-NC license therefore does not apply to this file. This file is released
under Apache-2.0 along with the rest of Repo2RLEnv. See LICENSE at repo root.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass


_HUNK_HEADER_RE = re.compile(r"^@@.*@@")
_FILE_HEADER_RE = re.compile(r"^(?:---|\+\+\+) ")
_INDEX_LINE_RE = re.compile(r"^index ")
_DIFF_GIT_RE = re.compile(r"^diff --git ")


@dataclass(slots=True)
class DiffRewardMetadata:
    similarity: float
    pred_lines: int
    oracle_lines: int
    matched_lines: int
    parse_error: str | None = None


def _normalize_diff(diff: str) -> list[str]:
    """Strip volatile metadata (hunk line numbers, indices, file headers context)."""
    lines: list[str] = []
    for line in diff.splitlines():
        if _DIFF_GIT_RE.match(line):
            continue
        if _INDEX_LINE_RE.match(line):
            continue
        if _HUNK_HEADER_RE.match(line):
            lines.append("@@")  # keep as a separator but drop line numbers
            continue
        if _FILE_HEADER_RE.match(line):
            # Keep filename markers but normalize whitespace
            lines.append(line.split("\t")[0].strip())
            continue
        lines.append(line)
    return lines


def calculate_diff_similarity_reward(
    oracle_diff: str, predicted_diff: str
) -> tuple[float, DiffRewardMetadata]:
    """Score a predicted diff against an oracle diff.

    Returns (reward, metadata) where reward ∈ [0, 1]:
      - 1.0 if normalized diffs are identical
      - 0.0 if predicted_diff is empty or unparseable
      - else difflib.SequenceMatcher ratio over normalized lines
    """
    if not predicted_diff.strip():
        return 0.0, DiffRewardMetadata(0.0, 0, 0, 0, "empty prediction")

    oracle_lines = _normalize_diff(oracle_diff)
    pred_lines = _normalize_diff(predicted_diff)

    if not oracle_lines:
        return 0.0, DiffRewardMetadata(
            0.0, len(pred_lines), 0, 0, "empty oracle after normalization"
        )

    matcher = difflib.SequenceMatcher(a=oracle_lines, b=pred_lines, autojunk=False)
    ratio = matcher.ratio()
    matched = sum(triple.size for triple in matcher.get_matching_blocks())

    return ratio, DiffRewardMetadata(
        similarity=ratio,
        pred_lines=len(pred_lines),
        oracle_lines=len(oracle_lines),
        matched_lines=matched,
    )


# ----------------------------------------------------------------------------
# SWE-bench-style test-execution grading (used by pr_runtime + Harbor verifiers)
# ----------------------------------------------------------------------------


@dataclass(slots=True)
class ExecutionReport:
    """Per-task report after running the model patch through the eval script.

    Mirrors SWE-bench's report shape so we can interop with their tooling.
    """

    fail_to_pass_success: list[str]
    fail_to_pass_failure: list[str]
    pass_to_pass_success: list[str]
    pass_to_pass_failure: list[str]

    @property
    def f2p_rate(self) -> float:
        total = len(self.fail_to_pass_success) + len(self.fail_to_pass_failure)
        return 1.0 if total == 0 else len(self.fail_to_pass_success) / total

    @property
    def p2p_rate(self) -> float:
        total = len(self.pass_to_pass_success) + len(self.pass_to_pass_failure)
        return 1.0 if total == 0 else len(self.pass_to_pass_success) / total

    @property
    def resolution_status(self) -> str:
        """SWE-bench's FULL / PARTIAL / NO labelling."""
        f2p, p2p = self.f2p_rate, self.p2p_rate
        if f2p == 1.0 and p2p == 1.0:
            return "FULL"
        if 0.0 < f2p < 1.0 and p2p == 1.0:
            return "PARTIAL"
        return "NO"


def grade_test_execution(
    fail_to_pass: list[str],
    pass_to_pass: list[str],
    test_status: dict[str, str],
) -> ExecutionReport:
    """Compute the per-task report from the post-prediction test status map.

    Args:
        fail_to_pass: oracle list of tests that must transition FAIL → PASS
        pass_to_pass: oracle list of tests that must stay PASSED
        test_status: parser output {test_name -> {PASSED|FAILED|SKIPPED|ERROR}}

    Tests not present in `test_status` count as failures (silent skip).
    """
    f2p_success, f2p_failure = [], []
    for t in fail_to_pass:
        (f2p_success if test_status.get(t) == "PASSED" else f2p_failure).append(t)
    p2p_success, p2p_failure = [], []
    for t in pass_to_pass:
        (p2p_success if test_status.get(t) == "PASSED" else p2p_failure).append(t)
    return ExecutionReport(
        fail_to_pass_success=f2p_success,
        fail_to_pass_failure=f2p_failure,
        pass_to_pass_success=p2p_success,
        pass_to_pass_failure=p2p_failure,
    )
