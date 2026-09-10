"""Remote-only procedural generation, reusing Repo2RLEnv's bootstrap/cache.

Acknowledgment: SWE-smith (MIT), pinned in provenance.md. Owned single-site
mutation enumeration and strict JUnit identity comparison replace the upstream
profile registry and validator, while retaining mutation/test/issue generation.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
from pathlib import Path

from repo2rlenv.campaigns.events import EventJournal, ProgressEvent
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.execution.python_repository import bootstrap_snapshot, test_image
from repo2rlenv.pipelines.recipes.swe_smith.mutations import generate_mutations
from repo2rlenv.pipelines.recipes.swe_smith.options import SWESmithOptions
from repo2rlenv.quality.test_results import execution_contrast
from repo2rlenv.spec.input import RepoSpec
from repo2rlenv.ui import console


def generate(repo: RepoSpec, options: SWESmithOptions, destination: Path) -> dict:
    if os.environ.get("REPO2RLENV_REMOTE_WORKER") != "1":
        raise RuntimeError("This entry point runs inside a remote generation worker only")
    destination.mkdir(parents=True, exist_ok=False)
    journal = EventJournal(destination / "events.jsonl")

    def event(stage, state, message, **metrics):
        journal.emit(
            ProgressEvent(
                recipe="swe_smith", stage=stage, state=state, message=message, metrics=metrics
            )
        )

    event(
        "bootstrap", "started", "Build the explicit repository profile using the existing bootstrap"
    )
    boot, base = bootstrap_snapshot(repo, options, destination)
    healthy = test_image(boot.image_digest, options, destination / "healthy")
    if healthy.returncode != 0 or not healthy.passed:
        raise ValueError("Healthy repository tests must pass before mutation")
    event(
        "bootstrap",
        "completed",
        "Fresh healthy test suite passed",
        healthy_tests=len(healthy.passed),
    )

    paths = []
    for relative in options.source_paths:
        path = base / relative
        paths.extend([path] if path.is_file() else sorted(path.rglob("*.py")))
    paths = sorted(set(paths))
    random.Random(options.seed).shuffle(paths)
    records, rejected, entity_counts = [], [], {}
    attempted = 0
    for path in paths:
        relative = path.relative_to(base).as_posix()
        original = path.read_text()
        candidates = generate_mutations(
            original, relative, seed=options.seed, limit=options.max_candidates
        )
        for candidate in candidates:
            key = (relative, candidate.entity)
            if entity_counts.get(key, 0) >= options.max_per_entity:
                continue
            if attempted >= options.max_candidates or len(records) >= options.target:
                break
            attempted += 1
            output = destination / "candidates" / candidate.id
            output.mkdir(parents=True)
            (output / "mutated.py").write_text(candidate.source)
            (output / "original.py").write_text(original)
            (output / "mutation.diff").write_text(candidate.patch)
            event(
                "contrast",
                "started",
                candidate.entity,
                attempted=attempted,
                execution_passed=len(records),
            )
            try:
                defective = test_image(
                    boot.image_digest,
                    options,
                    output / "defective",
                    replacement=(output / "mutated.py", relative),
                )
                contrast = execution_contrast(healthy, defective)
            except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                rejected.append(
                    {"id": candidate.id, "reason": type(exc).__name__, "detail": str(exc)}
                )
                event("contrast", "failed", str(exc))
                continue
            record = {
                "id": candidate.id,
                "source_file": relative,
                "entity": candidate.entity,
                "operator": candidate.operator,
                "line": candidate.line,
                "repo": repo.url,
                "ref": boot.ref,
                "image_digest": boot.image_digest,
                "contrast": contrast,
            }
            save_record(output / "candidate.json", record)
            records.append(record)
            entity_counts[key] = entity_counts.get(key, 0) + 1
            event(
                "contrast",
                "completed",
                candidate.entity,
                attempted=attempted,
                execution_passed=len(records),
            )
            save_record(
                destination / "generation.json",
                {"candidates": records, "rejected": rejected, "attempted": attempted},
            )
        if attempted >= options.max_candidates or len(records) >= options.target:
            break
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
        SWESmithOptions.model_validate(data["options"]),
        args.output,
    )
    console.json({"attempted": result["attempted"], "execution_passed": len(result["candidates"])})


if __name__ == "__main__":
    main()
