"""In-container verifier for pr_diff tasks (6-component reward).

This module is the **standalone verifier** that runs inside the task's
Docker container, NOT a helper used at generation time. It is read as
source at generation time, base64-encoded, and embedded into
``tests/test.sh``. At run time the container decodes it back to a file
and invokes it as ``python3 verifier.py <oracle> <predicted> <instruction>``.

Reward = weighted sum of 6 components (5 deterministic + 1 optional LLM):

  format_valid    (0 or 1)      — predicted parses as a unified diff (guard)
  size_sanity     ([0, 1])      — min(oracle_loc, pred_loc) / max(...)
                                  catches "rampage through the codebase"
  file_targeting  ([0, 1])      — F1 over changed-file sets (NOT Jaccard —
                                  symmetric penalty over-punishes extras)
  region_overlap  ([0, 1])      — predicted hunks overlap oracle hunks
                                  (strongest spatial-localization signal)
  similarity      ([0, 1])      — SequenceMatcher ratio over +/- lines only
                                  (no free credit for context lines)
  llm_judge       ([0, 1] or null) — Haiku rates "does this address the issue?"
                                     null on API failure / missing key →
                                     remaining weights are re-normalized

Default weights: 0.00 / 0.08 / 0.12 / 0.20 / 0.10 / 0.50 (retuned from
the original 0.05 / 0.05 / 0.10 / 0.20 / 0.20 / 0.40 after a 23-task
pilot — see the comment block above ``_DEFAULT_WEIGHTS`` below).
Override per-task via ``task.toml.metadata`` or per-run via env vars
``R2E_W_FORMAT`` / ``R2E_W_SIZE`` / ``R2E_W_FILE`` / ``R2E_W_REGION`` /
``R2E_W_SIM`` / ``R2E_W_JUDGE`` (pass via harbor ``--ve`` so they
reach the verifier container).

Final reward is clipped to [0, 1] and additionally clamped to ≤ 0.40
when ``size_sanity < 0.10`` (catastrophic-size hard cap — stops a
charitable judge from inflating scores on wildly wrong-sized patches).

Outputs:
  /logs/verifier/reward.txt   — single float (Harbor reads this)
  /logs/verifier/reward.json  — full component breakdown + status

Pure stdlib — uses only ``difflib``, ``json``, ``os``, ``re``,
``urllib``, ``sys``. The same module is imported by the unit tests
under ``tests/test_pr_diff_verifier.py`` so the in-container behavior
stays in lockstep with what we test.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Parsing helpers (shared by multiple components)
# ---------------------------------------------------------------------------

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FILE_MARKER_RE = re.compile(r"^(?:---|\+\+\+) ")
_INDEX_RE = re.compile(r"^index ")


def file_paths(diff: str) -> set[str]:
    """Return the set of ``b/<path>`` paths touched by a unified diff."""
    paths: set[str] = set()
    for line in diff.splitlines():
        m = _DIFF_HEADER_RE.match(line)
        if m:
            paths.add(m.group(2))
    return paths


def hunk_ranges(diff: str) -> dict[str, list[tuple[int, int]]]:
    """For each touched file, return list of (start_line, end_line) ranges
    on the **post-change** side (the ``+`` numbers in ``@@ -X +Y @@``).
    """
    out: dict[str, list[tuple[int, int]]] = {}
    current_file: str | None = None
    for line in diff.splitlines():
        m = _DIFF_HEADER_RE.match(line)
        if m:
            current_file = m.group(2)
            out.setdefault(current_file, [])
            continue
        m = _HUNK_HEADER_RE.match(line)
        if m and current_file is not None:
            new_start = int(m.group(3))
            new_count = int(m.group(4) or "1")
            end = new_start + max(new_count - 1, 0)
            out[current_file].append((new_start, end))
    return out


def _normalize_changes_only(diff: str) -> list[str]:
    """Return the +/- lines (the actual changes) for similarity scoring.

    Drops:
      - ``diff --git`` / ``index abc..def`` metadata
      - ``@@ ... @@`` hunk markers (volatile line numbers)
      - context lines (no +/- prefix) — these are unchanged code, scoring
        them inflates similarity for trivial patches
    Keeps file-marker lines (``--- a/foo`` / ``+++ b/foo``) so file identity
    stays in the comparison; normalizes off the tab+timestamp suffix git
    sometimes appends.
    """
    out: list[str] = []
    for line in diff.splitlines():
        if _DIFF_HEADER_RE.match(line) or _INDEX_RE.match(line):
            continue
        if _HUNK_HEADER_RE.match(line):
            continue
        if _FILE_MARKER_RE.match(line):
            out.append(line.split("\t")[0].strip())
            continue
        # +/- lines (actual changes)
        if line.startswith("+") or line.startswith("-"):
            out.append(line)
    return out


# ---------------------------------------------------------------------------
# Reward components
# ---------------------------------------------------------------------------


def format_valid(predicted: str) -> float:
    """1.0 if the predicted text looks like a unified diff; else 0.0.

    Requires both a ``diff --git`` header and at least one real +/- line
    (not the ``---`` / ``+++`` file-marker lines).
    """
    if not predicted.strip():
        return 0.0
    has_header = False
    has_change = False
    for line in predicted.splitlines():
        if _DIFF_HEADER_RE.match(line):
            has_header = True
        elif (line.startswith("+") or line.startswith("-")) and not line.startswith(
            ("+++ ", "--- ")
        ):
            has_change = True
        if has_header and has_change:
            return 1.0
    return 0.0


def file_targeting(oracle: str, predicted: str) -> float:
    """F1 over the changed-file sets.

    Why F1 instead of Jaccard:
      - Missing an oracle file (FN) is a *big* failure.
      - Touching one unrelated extra file (FP) is a small one.
      - Jaccard punishes both equally, which over-penalizes the agent
        for any extra exploration.
      - F1 weights TP twice, giving partial credit when the agent finds
        most-but-not-all of the oracle files even with some noise.

    Returns 1.0 if both sides touch zero files (degenerate).
    """
    o = file_paths(oracle)
    p = file_paths(predicted)
    if not o and not p:
        return 1.0
    tp = len(o & p)
    fn = len(o - p)
    fp = len(p - o)
    if tp == 0:
        return 0.0
    # F1 = 2·TP / (2·TP + FN + FP)
    return (2 * tp) / (2 * tp + fn + fp)


def _count_loc_changes(diff: str) -> int:
    """Number of real +/- lines (excluding file-marker lines)."""
    n = 0
    for line in diff.splitlines():
        if (line.startswith("+") or line.startswith("-")) and not line.startswith(("+++ ", "--- ")):
            n += 1
    return n


def size_sanity(oracle: str, predicted: str) -> float:
    """Penalize predicted patches that are wildly larger / smaller than the oracle.

    Returns ``min(oracle_loc, predicted_loc) / max(oracle_loc, predicted_loc)``.
    Both empty → 1.0 (degenerate but well-defined). One empty → 0.0.

    This is the "don't rampage through the codebase" guard — a 200-line
    diff for a 3-line oracle scores ~0.015 here even if every other
    component looks OK.
    """
    o_loc = _count_loc_changes(oracle)
    p_loc = _count_loc_changes(predicted)
    if o_loc == 0 and p_loc == 0:
        return 1.0
    if o_loc == 0 or p_loc == 0:
        return 0.0
    return min(o_loc, p_loc) / max(o_loc, p_loc)


def region_overlap(oracle: str, predicted: str, *, slack_lines: int = 5) -> float:
    """For each oracle hunk, did the predicted diff edit a line within
    ``slack_lines`` of that hunk in the same file?

    Returns the fraction of oracle hunks that have at least one matching
    predicted hunk. ``slack_lines`` tolerates slight line-number drift
    when git diff renders the predicted hunk at a different offset.
    """
    o_ranges = hunk_ranges(oracle)
    p_ranges = hunk_ranges(predicted)
    total = sum(len(v) for v in o_ranges.values())
    if total == 0:
        return 1.0  # oracle has no hunks (degenerate)
    matched = 0
    for fname, ranges in o_ranges.items():
        p_in_file = p_ranges.get(fname, [])
        for o_start, o_end in ranges:
            for p_start, p_end in p_in_file:
                # Interval overlap with slack — max(starts) <= min(ends) + slack
                if max(o_start, p_start) <= min(o_end, p_end) + slack_lines:
                    matched += 1
                    break
    return matched / total


def similarity(oracle: str, predicted: str) -> float:
    """SequenceMatcher ratio over +/- lines only.

    No free credit for context lines — fixes the v0.8.1 inflation where a
    one-line fix scored ~0.7 just because the 3 context lines on either
    side were "matched".
    """
    if not predicted.strip():
        return 0.0
    o = _normalize_changes_only(oracle)
    p = _normalize_changes_only(predicted)
    if not o:
        return 0.0
    return difflib.SequenceMatcher(a=o, b=p, autojunk=False).ratio()


# ---------------------------------------------------------------------------
# LLM-as-judge (Anthropic Messages API, stdlib urllib)
# ---------------------------------------------------------------------------


_JUDGE_PROMPT = (
    "You are scoring a code-fix patch on whether it logically addresses the "
    "described issue. Output a single JSON object: "
    '{{"score": <float in [0,1]>, "reasoning": "<<=30 words"}}.\n\n'
    "DO NOT use the oracle to grade. Score on:\n"
    "  - Does the predicted diff touch code regions that could plausibly fix the issue?\n"
    "  - Is the change direction consistent with what the issue describes?\n"
    "  - Is it a real attempt (not boilerplate, not no-op)?\n\n"
    "Issue:\n{instruction}\n\n"
    "Oracle (reference fix for comparison only — don't grade on similarity to it):\n"
    "{oracle}\n\n"
    "Predicted:\n{predicted}\n\n"
    "Return ONLY the JSON object."
)


_DEFAULT_JUDGE_MODEL = "claude-haiku-4-5-20251001"


def llm_judge(
    *,
    instruction: str,
    oracle: str,
    predicted: str,
    api_key: str,
    model: str = _DEFAULT_JUDGE_MODEL,
    timeout: int = 60,
) -> tuple[float | None, str]:
    """Return ``(score, status)``.

    ``score`` is a float in [0, 1] on success, ``None`` on failure.
    ``status`` is a short string: ``"ok"`` / ``"no_api_key"`` /
    ``"empty_predicted"`` / ``"network"`` / ``"parse"`` /
    ``"missing_score"``. Caller redistributes the judge weight if score
    is None.
    """
    if not api_key:
        return None, "no_api_key"
    if not predicted.strip():
        return 0.0, "empty_predicted"

    # Truncate hard so we don't blow up the prompt for big diffs
    prompt = _JUDGE_PROMPT.format(
        instruction=instruction[:4000],
        oracle=oracle[:4000],
        predicted=predicted[:4000],
    )
    body = json.dumps(
        {
            "model": model,
            "max_tokens": 200,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None, "network"

    try:
        payload = json.loads(raw)
        text = payload["content"][0]["text"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None, "parse"

    # Pull the first JSON object out of the model's response
    m = re.search(r"\{[^{}]*\"score\"[^{}]*\}", text, re.DOTALL)
    if not m:
        return None, "missing_score"
    try:
        obj = json.loads(m.group(0))
        score = float(obj.get("score", 0))
    except (json.JSONDecodeError, ValueError, TypeError):
        return None, "missing_score"
    score = max(0.0, min(1.0, score))
    return score, "ok"


# ---------------------------------------------------------------------------
# Combination
# ---------------------------------------------------------------------------


def combine(components: dict[str, float | None], weights: dict[str, float]) -> float:
    """Combine the 5 components into a final scalar in [0, 1].

    If ``llm_judge`` is None (judge failed / disabled), redistribute its
    weight proportionally across the 4 deterministic components. This
    preserves the [0, 1] range without giving 0-credit for a missing
    judge.
    """
    available = [(k, w) for k, w in weights.items() if components.get(k) is not None]
    if not available:
        return 0.0
    total_w = sum(w for _, w in available)
    if total_w <= 0:
        return 0.0
    return sum(weights[k] * components[k] for k, _ in available) / total_w


# ---------------------------------------------------------------------------
# Entry point (invoked by tests/test.sh inside the container)
# ---------------------------------------------------------------------------


# Weights retuned after a 23-task pilot. Rationale (LLM-as-reward-engineer
# analysis grounded in per-task component data):
#
#   - format_valid → 0.00: was 1.0 across ALL 21 evaluated tasks. Zero
#     discriminative signal — its 0.05 weight was pure dead weight.
#   - similarity → 0.10: Pearson ~0.85 correlation with region_overlap.
#     Carrying both at 0.20 double-counts positional accuracy and
#     additionally penalizes legitimate alternative implementations.
#   - llm_judge → 0.50: only component capturing semantic correctness
#     independent of textual form; diverges informatively from
#     similarity on cases like tokenizers (judge=0.72, sim=0.08).
#   - region_overlap stays 0.20: strongest spatial-localization signal.
#   - file_targeting → 0.12: leading indicator, less correlated with the
#     rest than region/similarity are with each other.
#   - size_sanity → 0.08: useful outlier detector for catastrophic
#     over/under-generation (prettier=0.01, serde=0.08).
#
# Total: 0.00 + 0.08 + 0.12 + 0.20 + 0.10 + 0.50 = 1.00.
_DEFAULT_WEIGHTS = {
    "format_valid": 0.00,
    "size_sanity": 0.08,
    "file_targeting": 0.12,
    "region_overlap": 0.20,
    "similarity": 0.10,
    "llm_judge": 0.50,
}

# Hard cap: when size_sanity < this threshold, clamp final reward to
# ≤ CATASTROPHIC_SIZE_CAP. Prevents a charitable judge from inflating
# the score on patches that are wildly the wrong size (catastrophic
# over- or under-generation).
_CATASTROPHIC_SIZE_THRESHOLD = 0.10
_CATASTROPHIC_SIZE_CAP = 0.40


def _read_weights_from_env() -> dict[str, float]:
    """Override defaults via R2E_W_* env vars (set in container by harbor)."""
    overrides = {
        "format_valid": os.environ.get("R2E_W_FORMAT"),
        "size_sanity": os.environ.get("R2E_W_SIZE"),
        "file_targeting": os.environ.get("R2E_W_FILE"),
        "region_overlap": os.environ.get("R2E_W_REGION"),
        "similarity": os.environ.get("R2E_W_SIM"),
        "llm_judge": os.environ.get("R2E_W_JUDGE"),
    }
    out = dict(_DEFAULT_WEIGHTS)
    for k, v in overrides.items():
        if v is None:
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) < 3:
        print(
            "usage: verifier.py <oracle.patch> <predicted.patch> <instruction.md>",
            file=sys.stderr,
        )
        return 2

    oracle = _read_or_empty(args[0])
    predicted = _read_or_empty(args[1])
    instruction = _read_or_empty(args[2])
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    judge_model = os.environ.get("R2E_JUDGE_MODEL", _DEFAULT_JUDGE_MODEL)

    fv = format_valid(predicted)
    ss = size_sanity(oracle, predicted)
    ft = file_targeting(oracle, predicted)
    ro = region_overlap(oracle, predicted)
    sim = similarity(oracle, predicted)
    judge_score, judge_status = llm_judge(
        instruction=instruction,
        oracle=oracle,
        predicted=predicted,
        api_key=api_key,
        model=judge_model,
    )

    components: dict[str, float | None] = {
        "format_valid": fv,
        "size_sanity": ss,
        "file_targeting": ft,
        "region_overlap": ro,
        "similarity": sim,
        "llm_judge": judge_score,
    }
    weights = _read_weights_from_env()
    final = combine(components, weights)
    final = max(0.0, min(1.0, final))

    # Hard cap on catastrophic size mismatches. Without this, a charitable
    # judge can inflate the reward on patches that are dramatically the
    # wrong size (prettier scored 0.23 with size_sanity=0.011 — the judge
    # was lenient at 0.25 despite the patch being a near-no-op vs a
    # 500-line oracle). The cap kicks in only at the extremes.
    if ss < _CATASTROPHIC_SIZE_THRESHOLD:
        final = min(final, _CATASTROPHIC_SIZE_CAP)

    breakdown = {
        "final_reward": round(final, 6),
        "components": {k: (None if v is None else round(v, 6)) for k, v in components.items()},
        "weights": {k: round(v, 6) for k, v in weights.items()},
        "judge_model": judge_model if judge_status == "ok" else None,
        "judge_status": judge_status,
    }

    os.makedirs("/logs/verifier", exist_ok=True)
    with open("/logs/verifier/reward.txt", "w", encoding="utf-8") as f:
        f.write(f"{final:.6f}\n")
    with open("/logs/verifier/reward.json", "w", encoding="utf-8") as f:
        json.dump(breakdown, f, indent=2)

    # Also print to stdout so harbor's log captures the breakdown
    print(json.dumps(breakdown, indent=2))
    return 0


def _read_or_empty(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "combine",
    "file_paths",
    "file_targeting",
    "format_valid",
    "hunk_ranges",
    "llm_judge",
    "main",
    "region_overlap",
    "similarity",
    "size_sanity",
]
