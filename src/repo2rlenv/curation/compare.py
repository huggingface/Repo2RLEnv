from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import time
from importlib.metadata import version
from pathlib import Path

from repo2rlenv.curation.artifacts import digest_task, release_task
from repo2rlenv.curation.budget import Budget, BudgetExceeded
from repo2rlenv.curation.campaign import (
    ADMISSION_VERSION,
    CandidateDeferred,
    campaign_lock,
    curate_one,
    save,
)
from repo2rlenv.curation.external_agent import runtime_path
from repo2rlenv.curation.inference import inference_settings
from repo2rlenv.curation.models import CampaignConfig
from repo2rlenv.curation.sources import resolve_pr

RUNTIMES = ("langgraph", "pi", "opencode")
logger = logging.getLogger(__name__)
OUTCOMES = {
    "accepted",
    "rejected",
    "deferred",
    "infrastructure_failure",
    "execution_failure",
    "budget_exhausted",
}
RETRYABLE = {"infrastructure_failure", "execution_failure"}


def _json_hash(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def runtime_snapshot() -> dict:
    """Identify the actual controller and adapters, independently of package versions."""
    package = Path(__file__).parent
    paths = sorted(
        [
            *package.glob("*.py"),
            *package.glob("runtimes/*.mjs"),
            *package.glob("runtimes/package*.json"),
        ]
    )
    hashes = {
        p.relative_to(package).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths
    }
    return {
        "source_hash": _json_hash(hashes),
        "source_files": hashes,
        "versions": {name: version(name) for name in ("langgraph", "harbor", "modal", "litellm")},
        "node_dependencies": json.loads(runtime_path("pi").with_name("package.json").read_text())[
            "dependencies"
        ],
    }


def _check_row(row: dict, seeds: list[str]) -> tuple[str, str]:
    key = row["source"], row["runtime"]
    if key[0] not in seeds or key[1] not in RUNTIMES or row["status"] not in OUTCOMES:
        raise ValueError(f"Invalid comparison result: {key}")
    return key


def _attempt_key(row: dict) -> tuple[str, str, str]:
    return row["source"], row["runtime"], row["evidence_dir"]


def _check_accepted(
    row: dict, out: Path, source: dict, *, released: bool, allow_stale: bool = False
) -> Path:
    if row.get("admission_version") != ADMISSION_VERSION and not allow_stale:
        raise ValueError(
            f"Accepted cell needs admission revalidation: {row['source']} {row['runtime']}"
        )
    if row.get("id") != source["id"]:
        raise ValueError("Accepted cell source identity changed")
    path = out / "tasks" / row["runtime"] / source["id"] if released else Path(row["task_path"])
    if not path.is_dir() or path.is_symlink() or digest_task(path) != row.get("task_digest"):
        raise ValueError(f"Accepted task missing or changed: {path}")
    return path


def _archive_stale_admission(row: dict, out: Path, source: dict) -> Path:
    task = _check_accepted(row, out, source, released=False, allow_stale=True)
    released = out / "tasks" / row["runtime"] / source["id"]
    archived = (
        out
        / "superseded"
        / row["runtime"]
        / f"{source['id']}-admission-{row.get('admission_version', 'legacy')}-{row['task_digest']}"
    )
    if released.exists():
        _check_accepted(row, out, source, released=True, allow_stale=True)
        if archived.exists():
            raise ValueError("Both superseded and released task exist; inspect before resuming")
        archived.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(released, archived)
    elif archived.exists():
        # Recover an interruption after the atomic move but before report save.
        if archived.is_symlink() or digest_task(archived) != row["task_digest"]:
            raise ValueError("Superseded task changed")
    else:
        # Completion may have been recorded before the original release.
        release_task(task, archived)
    return archived


def _freeze_existing_sources(out: Path, protocol: dict) -> None:
    sources = protocol.setdefault("sources", {})
    hashes = protocol.setdefault("source_hashes", {})
    for url, source in sources.items():
        if source.get("url") != url or url not in protocol["seeds"]:
            raise ValueError("Invalid frozen comparison source")
        digest = _json_hash(source)
        if url in hashes and hashes[url] != digest:
            raise ValueError(f"Frozen comparison source changed: {url}")
        hashes[url] = digest
    # Legacy protocols did not freeze source metadata. Recover what authors
    # actually saw from their persisted inputs instead of querying GitHub again.
    for path in sorted((out / "candidates").glob("*/*/*/source.json")):
        source = json.loads(path.read_text())
        url = source["url"]
        if url not in protocol["seeds"]:
            continue
        digest = _json_hash(source)
        if url in hashes and hashes[url] != digest:
            raise ValueError(f"Existing cells used different source metadata: {url}")
        sources[url], hashes[url] = source, digest


def summarize(rows: list[dict]) -> dict:
    summary = {}
    for runtime in RUNTIMES:
        cells = [r for r in rows if r["runtime"] == runtime]
        scores = [r["score"] for r in cells if r.get("score") is not None]
        summary[runtime] = {
            "completed": len(cells),
            "accepted": sum(r["status"] == "accepted" for r in cells),
            "quality_rejected": sum(r["status"] == "rejected" for r in cells),
            "deferred": sum(r["status"] == "deferred" for r in cells),
            "infrastructure_failures": sum(r["status"] == "infrastructure_failure" for r in cells),
            "execution_failures": sum(r["status"] == "execution_failure" for r in cells),
            "budget_stops": sum(r["status"] == "budget_exhausted" for r in cells),
            "scored": len(scores),
            "mean_scored_quality": sum(scores) / len(scores) if scores else None,
            "charged_or_reserved_usd": sum(r["charged_or_reserved_usd"] for r in cells),
            "total_duration_sec": sum(
                r["duration_sec"] for r in cells if r.get("duration_sec") is not None
            ),
            "unknown_duration_cells": sum(r.get("duration_sec") is None for r in cells),
        }
    return summary


def write_report(out: Path, manifest: dict) -> None:
    manifest["summary"] = summarize(manifest["rows"])
    save(out / "comparison.json", manifest)
    lines = [
        "# Matched author-runtime comparison",
        "",
        "Source PR metadata is frozen across resumes. Models, tool capabilities and limits",
        "share one protocol. Controller/adaptor hashes and resume history are recorded",
        "in comparison.json so implementation changes across attempts remain visible.",
        "",
        "| Runtime | Completed | Accepted | Quality rejected | Deferred | Infrastructure | Execution | Budget stops | Scored | Mean scored quality | Accounted USD |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for runtime, stats in manifest["summary"].items():
        score = stats["mean_scored_quality"]
        lines.append(
            f"| {runtime} | {stats['completed']} | {stats['accepted']} | "
            f"{stats['quality_rejected']} | {stats['deferred']} | {stats['infrastructure_failures']} | "
            f"{stats['execution_failures']} | "
            f"{stats['budget_stops']} | {stats['scored']} | "
            f"{f'{score:.1f}' if score is not None else '—'} | {stats['charged_or_reserved_usd']:.2f} |"
        )
    lines.extend(
        [
            "",
            "Missing scores are not zero scores. A high score cannot override failed gates.",
            "Interrupted cells may resume from prior drafts; all attempts remain in their evidence tree.",
            "Recovered results with unknown duration show a dash. This small pilot",
            "does not establish statistical superiority. Inspect per-PR results and traces.",
            "Cloud costs are conservative estimates; provider invoices remain authoritative.",
            "",
            "| PR | Runtime | Outcome | Seconds | Accounted USD | Evidence |",
            "|---|---|---|---:|---:|---|",
        ]
    )
    for row in manifest["rows"]:
        relative = Path(row["evidence_dir"]).relative_to(out)
        duration = f"{row['duration_sec']:.0f}" if row.get("duration_sec") is not None else "—"
        lines.append(
            f"| {row['source']} | {row['runtime']} | {row['status']} | "
            f"{duration} | {row['charged_or_reserved_usd']:.2f} | [{relative}]({relative}) |"
        )
    if manifest.get("previous_attempts"):
        lines.extend(
            [
                "",
                "## Previous attempts",
                "",
                "These failures were explicitly retried and remain part of the experiment record.",
                "Current-row costs are cumulative for each PR/runtime cell and already include",
                "its earlier attempts. Historical amounts below are not added to totals again.",
                "",
                "| PR | Runtime | Previous outcome | Reason | Accounted through attempt USD | Evidence |",
                "|---|---|---|---|---:|---|",
            ]
        )
        for row in manifest["previous_attempts"]:
            relative = Path(row["evidence_dir"]).relative_to(out)
            reason = " ".join("; ".join(map(str, row.get("reasons", []))).split())
            if len(reason) > 180:
                reason = reason[:177] + "..."
            reason = reason.replace("|", "\\|")
            lines.append(
                f"| {row['source']} | {row['runtime']} | {row['status']} | {reason} | "
                f"{row['charged_or_reserved_usd']:.2f} | [{relative}]({relative}) |"
            )
    (out / "comparison.md").write_text("\n".join(lines) + "\n")


async def compare(
    seeds: list[str], out: Path, config: CampaignConfig, *, retry_failures: bool = False
) -> dict:
    """Three authors concurrently per PR, with one shared budget and fixed evaluators."""
    out = out.resolve()
    seeds = list(dict.fromkeys(seeds))
    for runtime in ("pi", "opencode"):
        runtime_path(runtime)
    with campaign_lock(out):
        protocol_path = out / "protocol.json"
        protocol = (
            json.loads(protocol_path.read_text())
            if protocol_path.exists()
            else {"seeds": seeds, "config": config.model_dump(), "runtimes": list(RUNTIMES)}
        )
        if (
            protocol["seeds"] != seeds
            or protocol["runtimes"] != list(RUNTIMES)
            or CampaignConfig.model_validate(protocol["config"]) != config
        ):
            raise ValueError("Comparison resume requires the same PRs, configuration and runtimes")
        _freeze_existing_sources(out, protocol)
        save(protocol_path, protocol)
        path = out / "comparison.json"
        snapshot = runtime_snapshot()
        snapshot["inference"] = {
            model: inference_settings(model)
            for model in {config.author_model, config.judge_model, *config.solver_models}
        }
        manifest = (
            json.loads(path.read_text())
            if path.exists()
            else {
                "status": "running",
                "rows": [],
                "in_progress": [],
                "expected_cells": len(seeds) * 3,
                "human_review": "pending",
                "versions": snapshot["versions"],
                "node_dependencies": snapshot["node_dependencies"],
            }
        )
        global_budget = Budget(out / "budget.json", config.budget_usd)
        previous_attempts = manifest.setdefault("previous_attempts", [])
        archived_attempts = {_attempt_key(row) for row in previous_attempts}
        rows = {}
        for row in manifest["rows"]:
            key = _check_row(row, seeds)
            if key in rows:
                raise ValueError(f"Duplicate completed comparison cell: {key}")
            rows[key] = row

        def scoped_budget(url: str, runtime: str) -> Budget:
            return Budget(
                global_budget.path,
                config.budget_usd,
                scope=f"{runtime}:{url}",
                scope_limit=config.max_candidate_usd,
            )

        # These files are the write-ahead completion records. A crash after
        # recording a result, during release, or before the report is replaced
        # must not spend another author/solver budget for that cell.
        durable = {}
        for result_path in sorted((out / "candidates").glob("*/*/*/comparison-result.json")):
            row = json.loads(result_path.read_text())
            key = _check_row(row, seeds)
            if _attempt_key(row) in archived_attempts:
                continue
            if key in durable and durable[key] != row:
                raise ValueError(f"Conflicting durable results for comparison cell: {key}")
            durable[key] = row
            if key in rows and rows[key] != row:
                raise ValueError(f"Manifest differs from durable comparison result: {key}")
            rows.setdefault(key, row)

        # Older controllers could release an accepted task before writing the
        # comparison record. An accepted verdict is already terminal evidence;
        # rejected per-revision verdicts are not terminal and cannot be recovered.
        for verdict_path in sorted((out / "candidates").glob("*/*/*/verdict.json")):
            verdict = json.loads(verdict_path.read_text())
            runtime = verdict_path.parents[2].name
            key = verdict.get("source"), runtime
            if verdict.get("status") != "accepted" or key in rows:
                continue
            row = {
                **verdict,
                "runtime": runtime,
                "evidence_dir": str(verdict_path.parent),
                "duration_sec": None,
                "charged_or_reserved_usd": scoped_budget(key[0], runtime).spent,
                "recovered_from": "accepted_verdict",
            }
            if _attempt_key(row) in archived_attempts:
                continue
            _check_row(row, seeds)
            rows[key] = row

        for key, row in list(rows.items()):
            source = protocol["sources"].get(row["source"])
            if source is None:
                raise ValueError(f"Cannot recover frozen source metadata: {row['source']}")
            expected_source_hash = protocol["source_hashes"][row["source"]]
            if row.get("source_digest", expected_source_hash) != expected_source_hash:
                raise ValueError(f"Completed cell used different source metadata: {row['source']}")
            if row["status"] == "accepted":
                if row.get("admission_version") != ADMISSION_VERSION:
                    archived = _archive_stale_admission(row, out, source)
                    previous_attempts.append(
                        {
                            **row,
                            "archived_at": time.time(),
                            "archived_task": str(archived),
                            "revalidation_required": ADMISSION_VERSION,
                        }
                    )
                    archived_attempts.add(_attempt_key(row))
                    del rows[key]
                    continue
                if row in manifest["rows"]:
                    _check_accepted(row, out, source, released=True)
                else:
                    task = _check_accepted(row, out, source, released=False)
                    if row.get("recovered_from") == "accepted_verdict":
                        save(Path(row["evidence_dir"]) / "comparison-result.json", row)
                    release_task(task, out / "tasks" / row["runtime"] / source["id"])
                    _check_accepted(row, out, source, released=True)

        if retry_failures:
            for key, row in list(rows.items()):
                if row["status"] in RETRYABLE:
                    if _attempt_key(row) not in archived_attempts:
                        previous_attempts.append({**row, "archived_at": time.time()})
                        archived_attempts.add(_attempt_key(row))
                    del rows[key]

        history = manifest.setdefault("runtime_history", [])
        if not history and path.exists():
            history.append(
                {
                    "source_hash": None,
                    "source_files": None,
                    "started_at": None,
                    "versions": manifest.get("versions"),
                    "node_dependencies": manifest.get("node_dependencies"),
                    "provenance": "legacy controller; implementation hash not recorded",
                }
            )
        history.append({**snapshot, "started_at": time.time(), "retry_failures": retry_failures})
        history_index = len(history) - 1
        manifest["rows"] = list(rows.values())
        manifest["status"], manifest["in_progress"] = "running", []
        manifest["charged_or_reserved_usd"] = global_budget.spent
        write_report(out, manifest)
        completed = set(rows)

        async def cell(source, runtime):
            key = (source["url"], runtime)
            if key in completed:
                return
            parent = out / "candidates" / runtime / source["id"]
            previous = sorted(parent.glob("**/task/contract.json"), key=lambda p: p.stat().st_mtime)
            root = parent / str(time.time_ns())
            budget = scoped_budget(source["url"], runtime)
            started = time.monotonic()
            manifest["in_progress"].append({"source": source["url"], "runtime": runtime})
            write_report(out, manifest)
            logger.info("Comparison %s: %s", runtime, source["url"])
            try:
                result = await curate_one(
                    source,
                    root,
                    config.model_copy(update={"author_runtime": runtime}),
                    budget,
                    seed_task=previous[-1].parent if previous else None,
                )
            except CandidateDeferred as exc:
                result = {"status": "deferred", "reasons": [str(exc)]}
            except BudgetExceeded as exc:
                result = {"status": "budget_exhausted", "reasons": [str(exc)]}
            except Exception as exc:
                logger.exception("Comparison cell failed: %s %s", runtime, source["url"])
                result = {
                    "status": "infrastructure_failure",
                    "reasons": [f"{type(exc).__name__}: {exc}"],
                }
            result.update(
                source=source["url"],
                runtime=runtime,
                evidence_dir=str(root),
                duration_sec=time.monotonic() - started,
                charged_or_reserved_usd=budget.spent,
                source_digest=protocol["source_hashes"][source["url"]],
                runtime_source_hash=snapshot["source_hash"],
                runtime_history_index=history_index,
                previous_attempts=[
                    row["evidence_dir"]
                    for row in previous_attempts
                    if (row["source"], row["runtime"]) == key
                ],
            )
            root.mkdir(parents=True, exist_ok=True)
            _check_row(result, seeds)
            if result["status"] == "accepted":
                _check_accepted(result, out, source, released=False)
            save(root / "comparison-result.json", result)
            if result["status"] == "accepted":
                release_task(Path(result["task_path"]), out / "tasks" / runtime / source["id"])
                _check_accepted(result, out, source, released=True)
            manifest["rows"].append(result)
            manifest["in_progress"].remove({"source": source["url"], "runtime": runtime})
            write_report(out, manifest)

        async def source_group(index: int, url: str) -> None:
            source = protocol["sources"].get(url)
            if source is None:
                source = await asyncio.to_thread(resolve_pr, url)
                if source.get("url") != url:
                    raise ValueError(
                        f"Resolved comparison source does not match requested URL: {url}"
                    )
                protocol["sources"][url] = source
                protocol["source_hashes"][url] = _json_hash(source)
                save(protocol_path, protocol)
            order = RUNTIMES[index % 3 :] + RUNTIMES[: index % 3]
            async with asyncio.TaskGroup() as group:
                for runtime in order:
                    group.create_task(cell(source, runtime))

        # Bound active PR groups while preserving all three matched authors
        # within each group. Their write-ahead reservations share one budget.
        slots = asyncio.Semaphore(config.concurrency)

        async def bounded_source(index: int, url: str) -> None:
            async with slots:
                await source_group(index, url)

        async with asyncio.TaskGroup() as group:
            for index, url in enumerate(seeds):
                group.create_task(bounded_source(index, url))
        manifest["status"] = "complete"
        manifest["charged_or_reserved_usd"] = global_budget.spent
        write_report(out, manifest)
        return manifest
