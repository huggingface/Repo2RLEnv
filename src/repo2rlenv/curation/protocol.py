"""Bounded submissions and explicit verification designs for controlled pilots."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field

from repo2rlenv.curation.models import Contract, StrictModel


class DraftLimitExceeded(RuntimeError):
    pass


class MechanicalLimitExceeded(DraftLimitExceeded):
    """Construction input corrections exhausted; no source suitability judgment."""


class MechanicalTracker:
    """Every failed input attempt counts, including identical invalid exports."""

    def __init__(self, path: Path, limit: int):
        self.path, self.limit = path, limit
        self.rows = json.loads(path.read_text()) if path.exists() else []

    def fail(self, task: Path, reason: str) -> None:
        if len(self.rows) >= self.limit:
            raise MechanicalLimitExceeded("Mechanical submission allowance exhausted")
        self.rows.append({"task": str(task), "reason": reason})
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.rows, indent=2))
        tmp.replace(self.path)
        if len(self.rows) >= self.limit:
            raise MechanicalLimitExceeded(
                "Mechanical submission allowance exhausted; final cause retained"
            )


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
        raise ValueError(
            "Verification design must cover every requirement exactly once. "
            "behaviors[].requirement must equal contract.requirements[].id, NOT its behavior text. "
            f"Expected IDs: {sorted(requirements)!r}; received: {[b.requirement for b in plan.behaviors]!r}"
        )
    for behavior in plan.behaviors:
        if set(behavior.tests) != set(requirements[behavior.requirement].tests):
            raise ValueError(
                f"Design tests must match their requirement {behavior.requirement!r}: expected function names {requirements[behavior.requirement].tests!r}; received {behavior.tests!r}"
            )
        if not set(behavior.mutations) <= {m.name for m in contract.mutations}:
            raise ValueError(
                f"Design names a missing negative control: use contract.mutations[].name, not rationale text. Available: {[m.name for m in contract.mutations]!r}; received: {behavior.mutations!r}"
            )
        if not set(behavior.equivalents) <= {m.name for m in contract.equivalents}:
            raise ValueError(
                f"Design names a missing alternative implementation: use contract.equivalents[].name, not rationale text. Available: {[m.name for m in contract.equivalents]!r}; received: {behavior.equivalents!r}"
            )


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


CONVERSION_AUTHOR = """
This run uses the conversion policy. Mechanical input corrections have a separate
bounded allowance from structurally complete semantic submissions.
verification-plan.json behaviors[].requirement must equal contract.requirements[].id,
NOT contract.requirements[].behavior. tests lists Python function names; mutations
and equivalents list contract control names, NOT descriptive rationale. Implement
the identifiers in the accepted design consistently in contract.json and the tests.
Expected outcomes and explanations belong in expected_result. The host records
both and all cost still counts. Generated Python bytecode backed by source is removed
from exported tests/solution; other binary review inputs must be corrected explicitly.
Required plan metadata is prepared from the accepted design. Never invent tests or
controls merely to satisfy its schema. Preserve every meaningful screened behavior.
Each distinct complete task sent to static review consumes a semantic submission,
including rejected specifications, verifier gaps and execution failures. Identical
complete content reuses that submission. Follow the numeric limits in the prompt;
a final failed submission terminates the run. Packaging errors do not prove that
the PR is unsuitable. Defer only with concrete evidence of the unsupported behavior,
resource or dependency. Difficulty is reported separately from correctness.
"""
