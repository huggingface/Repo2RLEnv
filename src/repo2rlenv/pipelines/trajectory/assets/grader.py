# ruff: noqa: B905, SIM102
"""Deterministic short-answer grader v2 — NO LLM judge.

Runs inside `trajectory_to_env` qa task images as tests/grader.py.

Tiers (all offline/deterministic):
  1. Exact (case-insensitive, whitespace-collapsed)
  2. Numeric (abs/rel tolerance from ATOL/RTOL; gated to clean-number golds)
     + percent<->fraction bridge (e.g. gold 96.00 == pred 0.9621)
  3. List (comma-separated): split, strip, order-insensitive; numeric-tolerant per element
     (fixes 'a, b' vs 'a,b' spacing and reordering)
  4. Math-Verify (symbolic/numeric equivalence)

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
Adapted from the deterministic grader shipped with SmolDataEnvs:

  https://huggingface.co/datasets/FineEnvs/SmolDataEnvs-harbor-train (MIT)

This file is a DERIVATIVE WORK: the grading tiers and their code come from
SmolDataEnvs. Repo2RLEnv adds strict answer types and bounded Decimal parsing;
trajectory tasks use these instead of the legacy permissive tiers. The MIT terms apply and are
recorded in THIRD_PARTY_NOTICES.md at the repo root.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_NUMERIC_RE = re.compile(r"-?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?")
_STRICT_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


@dataclass
class GradeResult:
    reward: float
    method: str


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _to_float(s: str):
    if not s:
        return None
    m = _NUMERIC_RE.search(str(s).replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _num_close(g, c, abs_tol, rel_tol) -> bool:
    return abs(g - c) <= abs_tol or abs(g - c) / max(abs(g), 1e-9) <= rel_tol


def _is_clean_number(s: str) -> bool:
    t = (s or "").strip().strip("%$").strip().replace(",", "")
    return bool(_NUMERIC_RE.fullmatch(t))


def _elem_match(a, b, abs_tol, rel_tol) -> bool:
    if _normalize(a) == _normalize(b):
        return True
    fa, fb = _to_float(a), _to_float(b)
    if fa is not None and fb is not None:
        return _num_close(fa, fb, abs_tol, rel_tol)
    return False


def _list_match(gold: str, cand: str, abs_tol, rel_tol) -> bool:
    gl = [x.strip() for x in gold.split(",") if x.strip()]
    cl = [x.strip() for x in cand.split(",") if x.strip()]
    if len(gl) < 2 or len(gl) != len(cl):
        return False
    for gs, cs in ((gl, cl), (sorted(gl, key=str.lower), sorted(cl, key=str.lower))):
        if all(_elem_match(a, b, abs_tol, rel_tol) for a, b in zip(gs, cs)):
            return True
    return False


def _math_verify_match(gold: str, candidate: str) -> bool:
    try:
        from math_verify import parse, verify

        return bool(verify(parse(gold), parse(candidate), timeout_seconds=5))
    except Exception:
        return False


def grade(
    gold: str,
    candidate: str,
    *,
    question: str = "",
    reward_mode: str = "",
    judge: bool = True,
    judge_model=None,
    rel_tol: float = 1e-3,
    abs_tol: float = 1e-3,
) -> GradeResult:
    if not gold or candidate is None:
        return GradeResult(0.0, "miss")

    # Tasksmith-style typed answers: no inferred units, substring numbers or symbolic
    # fallback. A count of 50 is never equivalent to 0.5; labels are text, even with digits.
    if reward_mode.startswith("strict_"):
        kind = reward_mode.removeprefix("strict_")
        if kind == "number":
            if any(
                len(value.strip()) > 256 or not _STRICT_NUMBER.fullmatch(value.strip())
                for value in (gold, candidate)
            ):
                return GradeResult(0.0, "miss")
            try:
                g, c = Decimal(gold.strip()), Decimal(candidate.strip())
            except InvalidOperation:
                return GradeResult(0.0, "miss")
            matched = (
                g.is_finite()
                and c.is_finite()
                and max(abs(g.adjusted()), abs(c.adjusted())) <= 1000
                and (
                    abs(g - c) <= Decimal(str(abs_tol))
                    or abs(g - c) <= abs(g) * Decimal(str(rel_tol))
                )
            )
        elif kind == "list":
            matched = sorted(_normalize(x) for x in gold.split(",")) == sorted(
                _normalize(x) for x in candidate.split(",")
            )
        else:
            matched = _normalize(gold) == _normalize(candidate)
        return GradeResult(float(matched), "typed" if matched else "miss")

    # Tier 1: exact
    if _normalize(gold) == _normalize(candidate):
        return GradeResult(1.0, "exact")

    # Tier 2: numeric (clean-number gold) + percent/fraction bridge
    if reward_mode in ("numeric", "flexible") or _is_clean_number(gold):
        g, c = _to_float(gold), _to_float(candidate)
        if g is not None and c is not None:
            if _num_close(g, c, abs_tol, rel_tol):
                return GradeResult(1.0, "numeric")
            # percent<->fraction: one side is a fraction (<1), the other a percent (>=1)
            if (0 < abs(c) < 1 <= abs(g)) or (0 < abs(g) < 1 <= abs(c)):
                if _num_close(g, c * 100, abs_tol, rel_tol) or _num_close(
                    g, c / 100, abs_tol, rel_tol
                ):
                    return GradeResult(1.0, "numeric_scaled")

    # Tier 3: list (comma-separated), order-insensitive, per-element tolerant
    if reward_mode in ("list", "list_csv") or ("," in gold and "," in candidate):
        if _list_match(gold, candidate, abs_tol, rel_tol):
            return GradeResult(1.0, "list")

    # Tier 4: math-verify
    if _math_verify_match(gold, candidate):
        return GradeResult(1.0, "math_verify")

    return GradeResult(0.0, "miss")


# TOOL EFFICIENCY IS NOT COMPUTED HERE, AND CANNOT BE.
#
# This grader used to emit a `tool_efficiency` key derived from a tool-call count it read from
# `/workdir/.n_tool_calls` or `$N_TOOL_CALLS`. Nothing writes that file and no task.toml sets that
# variable, so the count was always None, the key was always null, and Harbor's reward model
# (`dict[str, float | int]`) rejected the WHOLE dict -- taking `correctness` down with it. The only
# path that survived was the empty-submission branch below, so the suite graded failures correctly
# and crashed on every successful answer. That silently removed 86 of 250 tasks from scoring.
#
# The count is not the sandbox's to know: it lives in the capture proxy, which sees every model call.
# A reward belongs in the verifier only if the sandbox is what makes it computable. Correctness needs
# the data, the gold answer and the tolerances -- it belongs here. Tool efficiency needs a trace --
# it belongs to whoever holds the trace, and it is computed there as a shaping term, gated on
# correctness so that "made no tool calls" can never outscore solving the task.


def _tols():
    def _f(name):
        try:
            return float(os.environ.get(name, "") or 1e-3)
        except ValueError:
            return 1e-3

    return _f("ATOL"), _f("RTOL")


def main_cli() -> int:
    gold = (os.environ.get("EXPECTED_ANSWER") or "").strip()
    question = (os.environ.get("QUESTION") or "").strip()
    candidate = sys.stdin.read().strip()
    at, rt = _tols()
    r = grade(
        gold,
        candidate,
        question=question,
        reward_mode=os.environ.get("REWARD_MODE", "") or "",
        abs_tol=at,
        rel_tol=rt,
    )
    print(f"{r.reward:.1f}")
    print(
        f"[grader] gold={gold!r} pred={candidate[:80]!r} reward={r.reward} method={r.method}",
        file=sys.stderr,
    )
    return 0


def main_json() -> int:
    import json

    gold = (os.environ.get("EXPECTED_ANSWER") or "").strip()
    question = (os.environ.get("QUESTION") or "").strip()
    candidate = sys.stdin.read().strip()
    if not candidate:
        print(json.dumps({"correctness": 0.0, "submission": 0.0}))
        return 0
    at, rt = _tols()
    r = grade(
        gold,
        candidate,
        question=question,
        reward_mode=os.environ.get("REWARD_MODE", "") or "",
        abs_tol=at,
        rel_tol=rt,
    )
    print(json.dumps({"correctness": float(r.reward), "submission": 1.0}))
    print(
        f"[grader] gold={gold!r} pred={candidate[:80]!r} correctness={r.reward} method={r.method}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    if "--json" in sys.argv[1:]:
        raise SystemExit(main_json())
    raise SystemExit(main_cli())
