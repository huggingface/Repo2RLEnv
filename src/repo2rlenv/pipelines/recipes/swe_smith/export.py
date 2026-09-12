"""Export a mutation as a standalone task with a separate Harbor verifier."""

from __future__ import annotations

from pathlib import Path

from repo2rlenv.pipelines.recipes.repository.export import export_repository_task
from repo2rlenv.pipelines.recipes.swe_smith.options import SWESmithOptions

RECIPE_VERSION = "1"
UPSTREAM_REVISION = "9b74ac08118a85c39c356802f7961893af73e07f"


def export_candidate(
    generation: Path,
    candidate: dict,
    options: SWESmithOptions,
    instruction: str,
    destination: Path,
    *,
    org: str,
    resume: bool = False,
) -> Path:
    """Both build contexts begin with defective source, including private tests.

    Only source files are collected into a fresh grading environment. Interpreter,
    tests, configuration and reward writer are never collected from the learner.
    """
    base = generation / "base"
    evidence = generation / "candidates" / candidate["id"]
    source_file = candidate["source_file"]
    return export_repository_task(
        base=base,
        defective={source_file: (evidence / "mutated.py").read_bytes()},
        reference={source_file: (evidence / "original.py").read_bytes()},
        options=options,
        instruction=instruction,
        destination=destination,
        name="swe-smith-" + candidate["id"],
        org=org,
        contrast=candidate["contrast"],
        metadata={
            "recipe": "swe_smith",
            "recipe_version": RECIPE_VERSION,
            "pipeline": "repo_mutate",
            "upstream_revision": UPSTREAM_REVISION,
            "repository": candidate["repo"],
            "source_revision": candidate["ref"],
            "mutation_id": candidate["id"],
        },
        single_reference=True,
        resume=resume,
    )
