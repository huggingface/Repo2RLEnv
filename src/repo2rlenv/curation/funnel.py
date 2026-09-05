"""Read-only inventory of retained curation evidence, not an admission decision.

The report counts distinct PRs separately from drafts, task digests and physical
trials. Costs come only from unique ledger entry IDs, never rollout copies.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_PR = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", re.I)
_SKIP = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "artifacts",
    "agent",
    "environment",
    "solution",
    "tests",
    "task",
    "tasks",
    "prior-review",
    "reference",
    "references",
    "systematic-reset",
}
_NAMES = {
    "source.json",
    "manifest.json",
    "result.json",
    "review.json",
    "verdict.json",
    "evidence.json",
    "comparison.json",
    "prepared.json",
    "started.json",
}
_STAGES = (
    "attempted",
    "draft",
    "revision",
    "verifier_review",
    "verifier_review_passed",
    "reference_trial",
    "reference_passed",
    "control_trial",
    "solver_trial",
    "solver_passed",
    "adversary_trial",
    "final_review",
    "raw_autoaccepted",
    "selected",
)


def source_url(value: Any) -> str | None:
    """Canonical public PR identity, ignoring refs, query strings and URL case."""
    if not isinstance(value, str):
        return None
    match = _PR.search(value)
    return (
        f"https://github.com/{match[1].lower()}/{match[2].lower()}/pull/{int(match[3])}"
        if match
        else None
    )


def _read(path: Path, skipped: list[dict], limit: int = 8_000_000) -> Any:
    try:
        if path.is_symlink() or path.stat().st_size > limit:
            skipped.append({"path": str(path), "reason": "symlink or size bound"})
            return None
        return json.loads(path.read_text())
    except (OSError, UnicodeError, ValueError) as exc:
        skipped.append({"path": str(path), "reason": type(exc).__name__})
        return None


def _inventory(root: Path) -> tuple[list[Path], list[Path], list[Path]]:
    files, tasks, author_roots = [], [], []
    for directory, dirs, names in os.walk(root, followlinks=False):
        folder = Path(directory)
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in _SKIP
            and not re.fullmatch(r"(?:production-|comparison-)?runtime-v\d+", d)
            and not (folder / d).is_symlink()
        )
        if folder.name == "trials":
            dirs[:] = []  # sidecars identify physical trials; never traverse exports/traces
        if (folder / "task" / "task.toml").is_file():
            tasks.append(folder)
        if "sandbox.json" in names or any(
            n.startswith("author-") and n.endswith(".jsonl") for n in names
        ):
            author_roots.append(folder)
        for name in sorted(names):
            if name == "review-submissions.json":
                continue
            if (
                name in _NAMES
                or re.fullmatch(r"provenance(?:-v\d+)?\.json", name)
                or (folder.name == "trials" and name.endswith(".json"))
            ):
                files.append(folder / name)
    return files, tasks, author_roots


def failure_labels(text: str) -> list[str]:
    """Multilabel text triage; unknown is retained rather than inventing a cause."""
    patterns = {
        "budget": r"budget|cost limit|spending cap",
        "infrastructure": r"timeout|timed out|transport|connection|sandbox|docker|modal|daytona|image build",
        "specification": r"ambigu|specification|instruction|answer leak",
        "verifier_coverage": r"coverage|untested|verifier|test gap|required repair",
        "difficulty": r"difficulty|too easy|transcription",
        "reference": r"oracle|reference implementation",
        "interrupted": r"interrupt|cancelled|sigint|sigterm",
    }
    labels = [name for name, pattern in patterns.items() if re.search(pattern, text, re.I)]
    return labels or ["unknown"]


def audit_funnel(
    workspace: Path,
    *,
    seed_paths: tuple[Path, ...] = (),
    ledger_paths: tuple[Path, ...] = (),
    selection_paths: tuple[Path, ...] = (),
) -> dict:
    """Build an evidence inventory without importing targets or changing any input.

    Explicit seed paths define the denominator; otherwise retained manifest seed
    lists are used. Ledger paths are authoritative in supplied order when copies
    disagree; every conflict remains visible. No provider-invoice claim is made.
    """
    workspace = Path(workspace).resolve()
    skipped: list[dict] = []
    files, tasks, author_roots = _inventory(workspace)
    docs = {path: data for path in files if isinstance(data := _read(path, skipped), dict)}
    sources: dict[str, dict] = {}
    roots: dict[Path, str] = {}
    scopes: dict[str, set[str]] = defaultdict(set)
    ids: dict[str, set[str]] = defaultdict(set)

    def row(url: str) -> dict:
        if url not in sources:
            sources[url] = {
                "source": url,
                "stages": set(),
                "evidence_paths": set(),
                "source_roots": set(),
                "author_attempt_roots": set(),
                "draft_paths": set(),
                "revision_paths": set(),
                "task_digests": set(),
                "manual_repair_interventions": [],
                "failure_labels": set(),
            }
        return sources[url]

    def direct(data: dict) -> str | None:
        return source_url(data.get("source")) or source_url(data.get("url"))

    # Source metadata and provenance connect repair/recovery folders to original PRs.
    for path, data in docs.items():
        url = direct(data)
        if path.name == "source.json" and url:
            roots[path.parent] = url
            row(url)["source_roots"].add(str(path.parent))
            row(url)["evidence_paths"].add(str(path))
            if isinstance(data.get("id"), str):
                ids[data["id"]].add(url)
    for path, data in docs.items():
        if not path.name.startswith("provenance"):
            continue
        source_path = data.get("source_path")
        if isinstance(source_path, str):
            source_doc = docs.get(Path(source_path))
            if source_doc is None and Path(source_path).is_file():
                source_doc = _read(Path(source_path), skipped)
            url = direct(source_doc) if isinstance(source_doc, dict) else None
            if url:
                roots.setdefault(path.parent, url)

    def owner(path: Path, data: dict | None = None) -> str | None:
        if data and (url := direct(data)):
            return url
        for parent in (path.parent, *path.parents):
            if parent in roots:
                return roots[parent]
        if data and isinstance(data.get("id"), str) and len(ids[data["id"]]) == 1:
            return next(iter(ids[data["id"]]))
        return None

    catalog: set[str] = set()
    catalog_evidence = []
    if seed_paths:
        for path in seed_paths:
            path = Path(path).resolve()
            try:
                if path.stat().st_size > 8_000_000:
                    raise ValueError("Seed file too large")
                urls = {source_url(m[0]) for m in _PR.finditer(path.read_text())}
                catalog.update(urls - {None})
                catalog_evidence.append({"path": str(path), "distinct_prs": len(urls)})
            except (OSError, UnicodeError, ValueError) as exc:
                skipped.append({"path": str(path), "reason": str(exc)})
    else:
        for path, data in docs.items():
            values = data.get("seeds")
            seeds = (
                {u for value in values if (u := source_url(value))}
                if isinstance(values, list)
                else set()
            )
            if seeds:
                catalog.update(seeds)
                catalog_evidence.append({"path": str(path), "distinct_prs": len(seeds)})
    for url in catalog:
        row(url)

    failures, acceptances, repairs = [], {}, set()
    trials: dict[tuple, dict] = {}
    final_reviews: dict[tuple, dict] = {}
    verifier_reviews: dict[tuple, dict] = {}

    def mark(url: str | None, stage: str, path: Path) -> None:
        if url:
            record = row(url)
            record["stages"].add(stage)
            record["evidence_paths"].add(str(path))
            if stage != "selected":
                record["stages"].add("attempted")

    def failed(url: str | None, path: Path, labels: list[str], detail: Any) -> None:
        failures.append({"source": url, "path": str(path), "labels": labels, "detail": detail})
        if url:
            row(url)["failure_labels"].update(labels)

    for folder in author_roots:
        url = owner(folder / "source.json")
        mark(url, "attempted", folder)
        if url:
            row(url)["author_attempt_roots"].add(str(folder))
    for folder in tasks:
        url = owner(folder / "task")
        stage = "draft" if "drafts" in folder.parts else "revision"
        mark(url, stage, folder / "task")
        if url:
            row(url)[stage + "_paths"].add(str(folder / "task"))
    # Bounded manifest summaries are evidence too; never recursively inspect arbitrary payloads.
    summaries = list(docs.items())
    for path, data in docs.items():
        for field in ("accepted", "rejected", "rows", "previous_attempts"):
            values = data.get(field)
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, dict):
                        summary = dict(value)
                        if field in {"accepted", "rejected"}:
                            summary.setdefault("status", field)
                        summaries.append((path, summary))
    for path, data in summaries:
        url = owner(path, data)
        if url and isinstance(data.get("budget_scope"), str):
            scopes[data["budget_scope"]].add(url)
        if url and isinstance(data.get("scope"), str):
            scopes[data["scope"]].add(url)
        scope_map = data.get("budget_scopes")
        for scope_url, scope in scope_map.items() if isinstance(scope_map, dict) else []:
            if isinstance(scope, str) and (u := source_url(scope_url)):
                scopes[scope].add(u)
        digest = data.get("task_digest") or data.get("repaired_task_digest")
        if url and isinstance(digest, str):
            row(url)["task_digests"].add(digest)
        if path.name.startswith("provenance") and url:
            before, after = data.get("original_task_digest"), data.get("repaired_task_digest")
            if before and after and before != after and "repair" in str(path.parent):
                key = (url, before, after)
                if key not in repairs:
                    repairs.add(key)
                    row(url)["manual_repair_interventions"].append(
                        {"before": before, "after": after, "path": str(path)}
                    )
        if "verifier-reviews" in path.parts and path.name == "result.json":
            # Cache directory identity deduplicates copied focused static reviews.
            key = (url, path.parent.name)
            review = data.get("review")
            if key not in verifier_reviews:
                verifier_reviews[key] = {
                    "source": url,
                    "path": str(path),
                    "status": data.get("status"),
                    "score": review.get("score") if isinstance(review, dict) else None,
                    "identity": path.parent.name,
                }
                mark(url, "verifier_review", path)
                if data.get("status") == "completed" and isinstance(review, dict):
                    score = review.get("score")
                    if (
                        isinstance(score, (int, float))
                        and score >= 3
                        and not review.get("blockers")
                    ):
                        mark(url, "verifier_review_passed", path)
                    else:
                        failed(url, path, ["verifier_review_rejected"], review.get("blockers", []))
                else:
                    failed(
                        url,
                        path,
                        failure_labels(str(data.get("error", "unknown"))),
                        data.get("error"),
                    )
        if isinstance(data.get("criteria"), dict) and path.name == "review.json":
            key = (url, digest or str(path.parent))
            final_reviews[key] = {"source": url, "path": str(path)}
            mark(url, "final_review", path)
            labels = [
                "quality_" + name
                for name, value in data["criteria"].items()
                if isinstance(value, dict) and value.get("outcome") == "fail"
            ]
            if labels or data.get("blockers"):
                failed(url, path, labels or ["unknown"], data.get("blockers", []))
        if data.get("status") in {
            "rejected",
            "execution_failure",
            "infrastructure_failure",
            "budget_exhausted",
            "interrupted",
            "deferred",
        }:
            detail = (
                data.get("reasons") or data.get("error") or data.get("feedback") or data["status"]
            )
            mark(url, "attempted", path)
            failed(url, path, failure_labels(json.dumps(detail)), detail)
        if data.get("status") == "accepted" and url:
            d = data.get("task_digest")
            key = (url, d or "unknown")
            acceptances.setdefault(key, {"source": url, "task_digest": d, "paths": []})[
                "paths"
            ].append(str(path))
            mark(url, "raw_autoaccepted", path)
        candidates = data.get("trials", []) if isinstance(data.get("trials", []), list) else []
        if isinstance(data.get("label"), str) and "reward" in data:
            candidates = [*candidates, data]
        for trial in candidates:
            if not isinstance(trial, dict) or not isinstance(trial.get("label"), str):
                continue
            u = owner(path, trial) or url
            label = trial["label"]
            physical = Path(str(trial.get("path", path))).name
            key = (u, trial.get("task_digest"), label, physical, trial.get("inference_digest"))
            if key in trials:
                trials[key]["evidence_paths"].append(str(path))
                continue
            trials[key] = {
                **{
                    k: trial.get(k)
                    for k in (
                        "label",
                        "task_digest",
                        "model",
                        "inference_digest",
                        "reward",
                        "error",
                        "path",
                    )
                },
                "source": u,
                "evidence_paths": [str(path)],
            }
            reward, error = trial.get("reward"), trial.get("error")
            if label.startswith("oracle"):
                mark(u, "reference_trial", path)
                if reward == 1 and not error:
                    mark(u, "reference_passed", path)
                elif not error:
                    failed(u, path, ["reference"], label)
            elif label.startswith("solver"):
                mark(u, "solver_trial", path)
                if reward == 1 and not error:
                    mark(u, "solver_passed", path)
                elif not error:
                    failed(u, path, ["solver_unsolved"], label)
            elif label == "adversary":
                mark(u, "adversary_trial", path)
                if reward == 1 and not error:
                    failed(u, path, ["adversary_rewarded_requires_trace_review"], label)
            else:
                mark(u, "control_trial", path)
                expected = 1 if label.startswith("equivalent-") else 0
                if reward != expected and not error:
                    failed(
                        u,
                        path,
                        ["valid_alternative_rejected" if expected else "negative_control_survived"],
                        label,
                    )
            if error:
                failed(u, path, failure_labels(str(error)), {"label": label, "error": error})

    selected = {}
    if not selection_paths and (workspace / "curation-final-selection.json").exists():
        selection_paths = (workspace / "curation-final-selection.json",)
    for path in selection_paths:
        path = Path(path).resolve()
        data = _read(path, skipped)
        if not isinstance(data, dict):
            continue
        for item in data.get("selected", []):
            if isinstance(item, dict) and (url := direct(item)):
                key = (url, item.get("task_digest"))
                selected[key] = {**item, "selection_evidence": str(path)}
                mark(url, "selected", path)

    # Unique ledger IDs prevent costs from being added again for resumed trial copies.
    if not ledger_paths:
        ledger_paths = tuple(sorted(workspace.glob("*/budget.json")))
    charges, conflicts = {}, []
    for ledger in ledger_paths:
        ledger = Path(ledger).resolve()
        data = _read(ledger, skipped, limit=64_000_000)
        if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
            continue
        for entry_id, entry in data["entries"].items():
            if not isinstance(entry, dict):
                continue
            if entry_id in charges:
                if charges[entry_id]["entry"] != entry:
                    conflicts.append(
                        {
                            "entry_id": entry_id,
                            "authoritative": charges[entry_id]["ledger"],
                            "other": str(ledger),
                        }
                    )
                charges[entry_id]["copies"].append(str(ledger))
                continue
            scope = entry.get("scope")
            candidates = {source_url(scope)} - {None}
            candidates.update(scopes.get(scope, set()))
            label = str(entry.get("label", ""))
            attribution = "source URL or manifest scope" if candidates else None
            if not candidates and isinstance(scope, str) and scope.startswith("repair:"):
                candidates.update(
                    u
                    for u in sources
                    if re.fullmatch(
                        re.escape("repair:" + u.split("/")[-3] + "-" + u.split("/")[-1])
                        + r"(?:-v\d+)?",
                        scope,
                    )
                )
                if candidates:
                    attribution = "unique repository basename and PR number in repair scope"
            if not candidates:
                candidates.update(u for name, urls in ids.items() if name in label for u in urls)
                if candidates:
                    attribution = "known source ID in entry label"
            u = next(iter(candidates)) if len(candidates) == 1 else None
            amount = entry.get("charged_usd")
            if not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
                skipped.append(
                    {"path": str(ledger), "reason": "invalid charge", "entry_id": entry_id}
                )
                continue
            charges[entry_id] = {
                "entry": entry,
                "source": u,
                "attribution": attribution if u else None,
                "ledger": str(ledger),
                "copies": [str(ledger)],
            }
    totals = Counter()
    per_source: dict[str, Counter] = defaultdict(Counter)
    unallocated = []
    for entry_id, charge in charges.items():
        entry = charge["entry"]
        bucket = {
            "metered": "metered_usd",
            "estimated": "estimated_usd",
            "reserved": "outstanding_reserved_usd",
        }.get(entry.get("status"), "unknown_status_usd")
        amount = entry["charged_usd"]
        totals[bucket] += amount
        if charge["source"]:
            per_source[charge["source"]][bucket] += amount
            if amount > 0:
                mark(charge["source"], "attempted", Path(charge["ledger"]))
        else:
            unallocated.append(
                {
                    "entry_id": entry_id,
                    "ledger": charge["ledger"],
                    "scope": entry.get("scope"),
                    "amount": amount,
                    "status": entry.get("status"),
                }
            )
    attempted = {u for u, item in sources.items() if "attempted" in item["stages"]}
    records = []
    for url, item in sorted(sources.items()):
        records.append(
            {
                **{k: sorted(v) if isinstance(v, set) else v for k, v in item.items()},
                "in_seed_catalog": url in catalog,
                "costs": dict(per_source[url]),
            }
        )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "workspace": str(workspace),
        "denominators": {
            "seed_catalog_distinct_prs": len(catalog),
            "attempted_catalog_prs": len(attempted & catalog),
            "untouched_catalog_prs": len(catalog - attempted),
            "attempted_outside_catalog_prs": len(attempted - catalog),
            "all_attempted_distinct_prs": len(attempted),
            "metadata_only_distinct_prs": len(set(sources) - attempted - catalog),
        },
        "catalog_evidence": catalog_evidence,
        "untouched_sources": sorted(catalog - attempted),
        "stage_distinct_sources": {s: sum(s in r["stages"] for r in records) for s in _STAGES},
        "counts": {
            "source_metadata_roots": sum(len(r["source_roots"]) for r in records),
            "author_attempt_roots": sum(len(r["author_attempt_roots"]) for r in records),
            "draft_artifacts": len(tasks) - sum("drafts" not in f.parts for f in tasks),
            "revision_artifacts": sum("drafts" not in f.parts for f in tasks),
            "unique_physical_trials": len(trials),
            "unattributed_physical_trials": sum(t["source"] is None for t in trials.values()),
            "unique_verifier_review_identities": len(verifier_reviews),
            "raw_autoaccepted_digests": len(acceptances),
            "selected_digests": len(selected),
            "recorded_manual_repair_transitions": len(repairs),
        },
        "sources": records,
        "trials": list(trials.values()),
        "final_reviews": list(final_reviews.values()),
        "verifier_reviews": list(verifier_reviews.values()),
        "raw_autoacceptances": list(acceptances.values()),
        "selected": list(selected.values()),
        "failures": failures,
        "failure_label_counts": dict(Counter(label for f in failures for label in f["labels"])),
        "failure_label_distinct_sources": dict(
            Counter(label for r in records for label in r["failure_labels"])
        ),
        "costs": {
            "unique_entry_ids": len(charges),
            "totals": dict(totals),
            "committed_usd": sum(totals.values()),
            "ledger_paths": [str(Path(p).resolve()) for p in ledger_paths],
            "conflicting_copies": conflicts,
            "unallocated_entries": unallocated,
            "unallocated_committed_usd": sum(e["amount"] for e in unallocated),
            "entries": charges,
        },
        "skipped_inputs": skipped,
        "limitations": [
            "A filesystem snapshot may race an active writer; malformed or oversized records remain listed as skipped.",
            "Stages count observed evidence, not necessarily passed gates; raw acceptance does not equal final selection or human approval.",
            "Attempts require author, task, trial, result or positive uniquely attributed ledger commitment evidence; resolving source metadata alone is not a completed attempt.",
            "Task copies in prior-review, releases, runtime trees and exported artifacts are excluded. Physical trials deduplicate by source/digest/label/trial-directory basename/inference digest.",
            "Repair transitions are inferred from separate repair provenance, not human labor counts. Revisions include validation and separate seed artifacts.",
            "Failure label counts are observed records and may repeat a cause across manifest copies; distinct-source counts are also supplied. Labels are multilabel evidence triage; expected negative-control rejection is not an environment failure. Unknowns remain visible.",
            "Charges are unique ledger commitments: metered, estimated and outstanding reservations are separate, not a provider invoice. Supplied ledger order resolves conflicting ID copies.",
            "Per-PR cost attribution requires a unique source URL, manifest scope mapping, unique repair repository/PR number or known source ID; unmatched entries remain unallocated.",
        ],
    }


def render_markdown(report: dict) -> str:
    """Render compact denominators and per-source drilldown links."""
    d, counts, costs = report["denominators"], report["counts"], report["costs"]
    lines = [
        "# Retained curation funnel",
        "",
        f"Snapshot: {report['generated_at']}",
        "",
        f"Seed catalog: **{d['seed_catalog_distinct_prs']} distinct PRs**. "
        f"Attempted: **{d['attempted_catalog_prs']}**; untouched: **{d['untouched_catalog_prs']}**. "
        f"Additional attempted PRs outside catalog: {d['attempted_outside_catalog_prs']}.",
        "",
        f"Raw autoaccepted task digests: **{counts['raw_autoaccepted_digests']}**. "
        f"Selected digests: **{counts['selected_digests']}**. "
        f"Recorded separate repair transitions: **{counts['recorded_manual_repair_transitions']}**.",
        "",
        "| Observed stage | Distinct sources |",
        "|---|---:|",
    ]
    lines += [
        f"| {stage} | {number} |" for stage, number in report["stage_distinct_sources"].items()
    ]
    lines += [
        "",
        "Ledger amounts are commitments, not invoices; trial copies are never summed as cost.",
        "",
        "| Accounting category | USD |",
        "|---|---:|",
    ]
    lines += [f"| {key} | {value:.4f} |" for key, value in costs["totals"].items()]
    lines += [
        f"| Total committed | {costs['committed_usd']:.4f} |",
        "",
        f"Unallocated commitments: ${costs['unallocated_committed_usd']:.4f} ({len(costs['unallocated_entries'])} entries); conflicting copied entries: {len(costs['conflicting_copies'])}.",
        "",
        "| PR | Stages reached | Separate repairs | Committed USD |",
        "|---|---|---:|---:|",
    ]
    for item in report["sources"]:
        if "attempted" in item["stages"]:
            label = item["source"].removeprefix("https://github.com/")
            lines.append(
                f"| [{label}]({item['source']}) | {', '.join(item['stages'])} | "
                f"{len(item['manual_repair_interventions'])} | {sum(item['costs'].values()):.4f} |"
            )
    lines += [
        "",
        "| Observed failure label | Distinct sources | Evidence records |",
        "|---|---:|---:|",
    ]
    lines += [
        f"| {label} | {report['failure_label_distinct_sources'].get(label, 0)} | {number} |"
        for label, number in sorted(report["failure_label_counts"].items())
    ]
    lines += ["", "## Limits", "", *[f"- {value}" for value in report["limitations"]], ""]
    return "\n".join(lines)


def write_report(report: dict, output: Path) -> tuple[Path, Path]:
    """Write derived output only, after the read-only audit has completed."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = output / "funnel.json", output / "funnel.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    markdown_path.write_text(render_markdown(report))
    return json_path, markdown_path
