"""Remote healthy tracing, development scheduling, and executable skeleton contrast."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from importlib.resources import files
from pathlib import Path

from repo2rlenv.campaigns.events import EventJournal, ProgressEvent
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.execution.python_repository import (
    TestInstrumentation,
    bootstrap_snapshot,
    test_image,
)
from repo2rlenv.pipelines.recipes.swe_flow.schedule import (
    development_schedule,
    function_catalog,
    skeletonize,
)
from repo2rlenv.quality.python_evidence import test_excerpts
from repo2rlenv.quality.test_results import execution_contrast
from repo2rlenv.spec.input import RepoSpec
from repo2rlenv.spec.recipe_options import ReconstructionOptions
from repo2rlenv.ui import console


def generate(repo: RepoSpec, options: ReconstructionOptions, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=False)
    journal = EventJournal(destination / "events.jsonl")

    def event(stage, state, message, **metrics):
        journal.emit(
            ProgressEvent(
                recipe="swe_flow", stage=stage, state=state, message=message, metrics=metrics
            )
        )

    event("bootstrap", "started", "Build a healthy repository and trace the configured tests")
    boot, base = bootstrap_snapshot(repo, options, destination)
    healthy = test_image(boot.image_digest, options, destination / "healthy")
    traced = test_image(
        boot.image_digest,
        options,
        destination / "tracing",
        instrumentation=TestInstrumentation(
            driver=Path(str(files(__package__).joinpath("trace_driver.py"))),
            arguments=(
                json.dumps(
                    {
                        "source_paths": options.source_paths,
                        "max_tests": options.trace_max_tests,
                        "seed": options.trace_seed,
                    }
                ),
            ),
            outputs=("traces.json",),
        ),
    )
    if healthy.returncode or not healthy.passed or traced.returncode or not traced.passed:
        raise ValueError("Healthy traced repository must pass before scheduling")
    catalog = function_catalog(base, options.source_paths)
    traces = json.loads((destination / "tracing/traces.json").read_text())
    schedules = development_schedule(traces, catalog)
    save_record(destination / "schedule.json", {"steps": schedules, "function_catalog": catalog})
    event(
        "schedule",
        "completed",
        "Grouped core dependencies and ordered development steps",
        steps=len(schedules),
    )
    records, rejected = [], []
    attempted = 0
    for schedule in schedules[: options.max_candidates]:
        if len(records) >= options.target:
            break
        attempted += 1
        identity = {"repo": repo.url, "ref": boot.ref, "schedule": schedule}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
        candidate = destination / "candidates" / key
        candidate.mkdir(parents=True)
        source_files = sorted({catalog[key]["path"] for key in schedule["nodes_to_develop"]})
        replacements = {}
        for relative in source_files:
            path = candidate / "defective-source" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                skeletonize((base / relative).read_text(), path=relative, schedule=schedule)
            )
            replacements[relative] = path
        event(
            "contrast",
            "started",
            f"Development step {schedule['step']}",
            attempted=attempted,
            execution_passed=len(records),
        )
        try:
            defective = test_image(
                boot.image_digest, options, candidate / "defective", replacements=replacements
            )
            contrast = execution_contrast(healthy, defective)
        except (ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            rejected.append({"id": key, "reason": type(exc).__name__, "detail": str(exc)})
            event("contrast", "failed", str(exc))
            continue
        record = {
            **identity,
            "id": key,
            "contrast": contrast,
            "source_files": source_files,
            "functions": {key: catalog[key] for key in schedule["nodes_to_develop"]},
            "test_evidence": test_excerpts(base, contrast["FAIL_TO_PASS"]),
        }
        save_record(candidate / "candidate.json", record)
        records.append(record)
        save_record(
            destination / "generation.json",
            {"candidates": records, "rejected": rejected, "attempted": attempted},
        )
        event("contrast", "completed", key, execution_passed=len(records))
    result = {"candidates": records, "rejected": rejected, "attempted": attempted}
    save_record(destination / "generation.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    data = json.loads(args.config.read_text())
    result = generate(
        RepoSpec.model_validate(data["repo"]),
        ReconstructionOptions.model_validate(data["options"]),
        args.output,
    )
    console.json({"attempted": result["attempted"], "execution_passed": len(result["candidates"])})


if __name__ == "__main__":
    main()
