"""Bounded submissions and explicit verification designs for controlled pilots."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field

from repo2rlenv.curation.models import Contract, StrictModel


class DraftLimitExceeded(RuntimeError):
    pass


class DraftTracker:
    """Count distinct submitted tasks, including structurally invalid drafts."""

    def __init__(self, path: Path, limit: int | None):
        self.path, self.limit = path, limit
        self.rows = json.loads(path.read_text()) if path.exists() else []

    def observe(self, digest: str, task: Path) -> None:
        if self.limit is None or any(row["digest"] == digest for row in self.rows):
            return
        if len(self.rows) >= self.limit:
            raise DraftLimitExceeded(
                f"Submitted draft limit {self.limit} exhausted; no further autonomous repairs"
            )
        self.rows.append({"digest": digest, "task": str(task)})
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.rows, indent=2))
        tmp.replace(self.path)


class BehaviorDesign(StrictModel):
    requirement: str
    expected_result: str = Field(min_length=30)
    tests: list[str] = Field(min_length=1)
    mutations: list[str] = Field(min_length=1)
    equivalents: list[str] = Field(min_length=1)


class VerificationPlan(StrictModel):
    behaviors: list[BehaviorDesign] = Field(min_length=2)
    offline_dependencies: str = Field(min_length=20)
    artifact_boundary: str = Field(min_length=20)


def check_verification_plan(task: Path, contract: Contract) -> None:
    if contract.reward_mode != "deterministic":
        raise ValueError("This controlled pilot requires deterministic rewards")
    path = task / "verification-plan.json"
    if not path.is_file() or path.is_symlink():
        raise ValueError("Missing regular verification-plan.json")
    plan = VerificationPlan.model_validate_json(path.read_text())
    requirements = {r.id: r for r in contract.requirements}
    if len(requirements) != len(contract.requirements):
        raise ValueError("Requirement IDs must be unique")
    if len(plan.behaviors) != len(requirements) or {b.requirement for b in plan.behaviors} != set(
        requirements
    ):
        raise ValueError("Verification design must cover every requirement exactly once")
    for behavior in plan.behaviors:
        if set(behavior.tests) != set(requirements[behavior.requirement].tests):
            raise ValueError("Design tests must match their requirement")
        if not set(behavior.mutations) <= {m.name for m in contract.mutations}:
            raise ValueError("Design names a missing negative control")
        if not set(behavior.equivalents) <= {m.name for m in contract.equivalents}:
            raise ValueError("Design names a missing alternative implementation")


PILOT_AUTHOR = """
This is a frozen pilot. You have an initial submission and at most ONE repaired
submission: two distinct exported task contents total, including structural failures.
Repeated submission of identical files uses the same draft. Any change after the
second submitted draft exhausts the limit. Do not use validate_candidate on partial
files. Explore and check syntax in your author sandbox before submitting.

Before implementing the verifier, write verification-plan.json in /output/task.
Its schema is {"behaviors": [{"requirement": "requirement id", "expected_result":
"Explain an independently computed expected result, its input family and boundaries",
"tests": ["test names matching this requirement"], "mutations": ["control names"],
"equivalents": ["control names"]}], "offline_dependencies": "Pinned dependencies
and offline fixtures needed", "artifact_boundary": "Editable paths and why these
include permitted helper modules"}. Cover every contract requirement exactly once.
Each behavior needs an executable plausible wrong implementation and a permitted
alternative. Controls may cover multiple related requirements. Expected results
must be independent of submitted implementation outputs. This plan is reviewed
alongside the actual assertions; prose alone does not establish correctness.
Preserve the substantive PR scope established by screening. Do not shrink the task
to obtain a pass. Defer with evidence if it cannot be verified honestly on CPU.
"""
