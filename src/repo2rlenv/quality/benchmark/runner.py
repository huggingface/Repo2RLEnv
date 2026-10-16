"""Paired static reviewer trials; hidden labels stay on the controller."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.locking import lock_file
from repo2rlenv.quality.benchmark.models import BenchmarkOptions, inventory, load_manifest
from repo2rlenv.quality.benchmark.scoring import score_case, summarize
from repo2rlenv.quality.loop.artifacts import snapshot
from repo2rlenv.quality.loop.runner import QualityLoop
from repo2rlenv.quality.reviewer.profile import PROFILE, content_hash, identity

PROFILES = ("practical-generation-v1", PROFILE)


def run_benchmark(
    manifest_path: Path,
    options: BenchmarkOptions,
    directory: Path,
    ledger: BudgetLedger,
    *,
    split: str,
    resume: bool = False,
    selection_report: Path | None = None,
    loop_factory=QualityLoop,
    on_event=None,
) -> dict:
    """Each profile/case/repeat owns receipts; resume never substitutes repeats."""
    manifest_path, directory = manifest_path.resolve(), directory.resolve()
    manifest = load_manifest(manifest_path)
    cases = [case for case in manifest.cases if case.split == split]
    if not cases:
        raise ValueError("No benchmark cases for this split")
    for case in manifest.cases:
        task = (manifest_path.parent / case.task).resolve()
        if directory == task or directory.is_relative_to(task):
            raise ValueError("Benchmark output must be outside every input task")
    reviewers = {
        profile: identity(options.review.model_copy(update={"profile": profile}))
        for profile in PROFILES
    }
    manifest_hash = content_hash(manifest.model_dump(mode="json"))
    candidate_hash = content_hash(reviewers[PROFILE])
    selection_hash = None
    if split == "final":
        if selection_report is None:
            raise ValueError(
                "Final split requires a completed --selection-report to pin the candidate"
            )
        selected = json.loads(selection_report.read_text())
        if (
            selected.get("state") != "completed"
            or selected.get("split") != "selection"
            or selected.get("manifest_hash") != manifest_hash
            or selected.get("candidate_hash") != candidate_hash
            or selected.get("options") != options.model_dump(mode="json")
            or any(not row["completed"] for row in selected.get("attempts", []))
            or not selected.get("attempts")
        ):
            raise ValueError("Selection report does not pin this completed candidate comparison")
        selection_hash = content_hash(selected)
    configuration = {
        "schema_version": "1",
        "manifest_hash": manifest_hash,
        "candidate_hash": candidate_hash,
        "reviewers": reviewers,
        "options": options.model_dump(mode="json"),
        "split": split,
        "selection_hash": selection_hash,
        "ledger": str(ledger.path.resolve()),
    }
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as handle:
        try:
            lock_file(handle)
        except BlockingIOError as exc:
            raise RuntimeError("Another controller owns this benchmark") from exc
        receipt = directory / "run.json"
        if receipt.exists():
            if not resume or json.loads(receipt.read_text()) != configuration:
                raise ValueError("Benchmark resume requires identical inputs, reviewer and options")
        else:
            save_record(receipt, configuration)
        save_record(directory / "manifest.json", manifest.model_dump(mode="json"))
        inputs = {}
        for case in cases:
            # Opaque directory names hide labels embedded in a curator's source path.
            target = snapshot(manifest_path.parent / case.task, directory / "inputs" / case.case_id)
            if inventory(target) != case.files:
                raise ValueError(f"Copied case changed: {case.case_id}")
            inputs[case.case_id] = target

        def attempt(case, profile, repeat):
            output = directory / "reviews" / profile / case.case_id / str(repeat)
            opts = options.review.model_copy(update={"profile": profile})
            error = None
            result = None
            try:
                result = loop_factory(opts, output, ledger).run(inputs[case.case_id], resume=resume)
            except (ValueError, OSError, RuntimeError) as exc:
                # Do not turn malformed/provider outputs into a task defect.
                error = {"type": type(exc).__name__, "message": str(exc)}
                save_record(output / "benchmark-error.json", error)
            row = dict(
                score_case(case, result),
                profile=profile,
                repeat=repeat,
                error=error,
                accounted_usd=result.accounted_usd if result else None,
                reserved_usd=result.reserved_usd if result else None,
            )
            save_record(output / "benchmark-score.json", row)
            return row

        rows = []
        jobs = [
            (case, profile, repeat)
            for case in cases
            for repeat in range(options.repeats)
            for profile in PROFILES
        ]

        def report(state):
            result = {
                "schema_version": "1",
                "state": state,
                "split": split,
                "manifest_hash": manifest_hash,
                "candidate_hash": candidate_hash,
                "options": options.model_dump(mode="json"),
                "scheduled_attempts": len(jobs),
                "attempts": sorted(rows, key=lambda r: (r["case_id"], r["repeat"], r["profile"])),
                "summary": summarize(rows, samples=2000 if state == "completed" else 0),
                "campaign_budget": {k: v for k, v in ledger.status().items() if k != "operations"},
            }
            save_record(directory / "report.json", result)
            return result

        report("running")
        with ThreadPoolExecutor(max_workers=options.concurrency) as executor:
            futures = [executor.submit(attempt, *job) for job in jobs]
            for future in as_completed(futures):
                row = future.result()
                rows.append(row)
                report("running")
                if on_event:
                    on_event(row, len(rows), len(jobs))
        return report("completed")
