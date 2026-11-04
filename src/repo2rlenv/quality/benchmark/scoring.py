"""Common-category/location metrics, with abstentions retained in denominators."""

from __future__ import annotations

import random
from collections import defaultdict


def localized(issue: dict, defect) -> bool:
    if issue["category"] != defect.category or issue["severity"] != "blocking":
        return False
    anchor = " ".join(defect.location.quote.split())
    for citation in issue["evidence"]:
        quote = " ".join(citation["quote"].split())
        if (
            citation["path"] == defect.location.path
            and min(len(quote), len(anchor)) >= 8
            and (quote in anchor or anchor in quote)
        ):
            return True
    return False


def score_case(case, result) -> dict:
    review = result.review if result else None
    issues = [i.model_dump() for i in review.issues] if review else []
    blocking = [i for i in issues if i["severity"] == "blocking"]
    decidable = bool(
        review
        and result.status not in {"needs_evidence", "budget_exhausted"}
        and (
            blocking
            or all(getattr(review, k).status != "unknown" for k in ["task", "verifier", "leakage"])
        )
    )
    return {
        "case_id": case.case_id,
        "family": case.family,
        "split": case.split,
        "label_status": case.audit.status,
        "defective": case.defect is not None,
        "decidable": decidable,
        "completed": review is not None,
        "detected": bool(case.defect and any(localized(i, case.defect) for i in issues)),
        "false_blocking": bool(case.defect is None and blocking),
        "blocking_findings": len(blocking),
        "status": result.status if result else "execution_error",
    }


def rates(rows):
    clean = [r for r in rows if not r["defective"]]
    defective = [r for r in rows if r["defective"]]
    return {
        "attempts": len(rows),
        "clean_attempts": len(clean),
        "defective_attempts": len(defective),
        "detected": sum(r["detected"] for r in defective),
        "false_blocked": sum(r["false_blocking"] for r in clean),
        "decidable": sum(r["decidable"] for r in rows),
        "execution_errors": sum(not r["completed"] for r in rows),
        "detection_rate": sum(r["detected"] for r in defective) / len(defective)
        if defective
        else None,
        "false_blocking_rate": sum(r["false_blocking"] for r in clean) / len(clean)
        if clean
        else None,
        "decision_coverage": sum(r["decidable"] for r in rows) / len(rows) if rows else None,
    }


def summarize(rows, *, samples=2000):
    by_profile = defaultdict(list)
    for row in rows:
        by_profile[row["profile"]].append(row)
    summary = {
        p: {
            "adjudicated": rates([r for r in rs if r["label_status"] == "adjudicated"]),
            "provisional": rates([r for r in rs if r["label_status"] != "adjudicated"]),
        }
        for p, rs in by_profile.items()
    }
    # Pair family resamples across profiles, retaining all variants and repeats.
    eligible = [r for r in rows if r["label_status"] == "adjudicated"]
    families = sorted({r["family"] for r in eligible})
    deltas = defaultdict(list)
    baseline, candidate = "practical-generation-v1", "evidence-review-v2"
    if len(families) >= 2 and set(by_profile) == {baseline, candidate}:
        grouped = {f: [r for r in eligible if r["family"] == f] for f in families}
        rng = random.Random(0)
        for _ in range(samples):
            picked = [r for f in rng.choices(families, k=len(families)) for r in grouped[f]]
            a, b = [rates([r for r in picked if r["profile"] == p]) for p in (baseline, candidate)]
            for metric in ("detection_rate", "false_blocking_rate", "decision_coverage"):
                if a[metric] is not None and b[metric] is not None:
                    deltas[metric].append(b[metric] - a[metric])
    intervals = {
        metric: [
            sorted(values)[int(0.025 * (len(values) - 1))],
            sorted(values)[int(0.975 * (len(values) - 1))],
        ]
        for metric, values in deltas.items()
    }
    return {
        "profiles": summary,
        "family_count": len(families),
        "paired_family_bootstrap_95pct_delta": intervals,
        "promotion": "manual_review_required",
        "finding_precision": None,
        "precision_note": "Finding precision requires independent issue adjudication; label matching is not precision.",
    }
