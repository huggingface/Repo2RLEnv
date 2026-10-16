"""Private benchmark manifest: labels never enter the reviewer's evidence pack."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from repo2rlenv.quality.loop.artifacts import digest, task_identity
from repo2rlenv.quality.loop.models import Citation, IssueCategory, LoopOptions, Record


class ExpectedDefect(Record):
    category: IssueCategory
    location: Citation
    explanation: str = Field(min_length=1)


class Audit(Record):
    status: Literal["provisional", "adjudicated"]
    reviewers: list[str] = Field(min_length=1)
    rationale: str = Field(min_length=1)
    evidence: list[Citation] = Field(min_length=1)

    @model_validator(mode="after")
    def independent_review(self):
        if len(set(self.reviewers)) != len(self.reviewers):
            raise ValueError("Audit reviewer identities must be distinct")
        if self.status == "adjudicated" and len(self.reviewers) < 2:
            raise ValueError("Adjudicated labels require two recorded reviewers")
        return self


class Case(Record):
    case_id: str = Field(pattern=r"^c-[a-f0-9]{12}$")
    family: str = Field(min_length=1)
    lineage: list[str] = Field(min_length=1)
    split: Literal["development", "selection", "final"]
    task: str
    bundle_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    # Physical digest also binds advisory metadata excluded from the executable hash.
    files: dict[str, str] = Field(min_length=1)
    defect: ExpectedDefect | None
    audit: Audit


class Manifest(Record):
    schema_version: Literal["1"] = "1"
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def families_are_disjoint(self):
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("Duplicate case ID")
        groups, lineages, bundles = {}, {}, {}
        for case in self.cases:
            if groups.setdefault(case.family, case.split) != case.split:
                raise ValueError("Task family crosses benchmark splits")
            for key in case.lineage:
                if lineages.setdefault(key, case.family) != case.family:
                    raise ValueError("Shared lineage must belong to one family")
            if bundles.setdefault(case.bundle_hash, case.family) != case.family:
                raise ValueError("Duplicate task bundle must belong to one family")
        return self


class BenchmarkOptions(Record):
    review: LoopOptions = Field(default_factory=lambda: LoopOptions(max_probes=0))
    repeats: int = Field(default=2, ge=1, le=10)
    concurrency: int = Field(default=4, ge=1, le=8)

    @model_validator(mode="after")
    def review_only(self):
        if self.review.repair or self.review.run_rollout or self.review.max_probes:
            raise ValueError(
                "Benchmark sessions review fixed evidence; no repair, solver or probes"
            )
        if self.review.escalation_model is not None:
            raise ValueError("Reviewer comparison holds the model fixed; no escalation")
        return self


def inventory(task: Path) -> dict[str, str]:
    """Only Harbor inputs are allowed; no truth/feedback sidecars or symlinks."""
    result = {}
    for p in sorted(task.rglob("*")):
        if p.is_symlink():
            raise ValueError("Benchmark task cannot contain symlinks")
        if not p.is_file():
            continue
        relative = p.relative_to(task)
        if relative.parts[0] not in {
            "instruction.md",
            "task.toml",
            "environment",
            "solution",
            "tests",
        }:
            raise ValueError(f"Non-Harbor evidence in benchmark task: {relative}")
        result[relative.as_posix()] = digest(p)
    return result


def load_manifest(path: Path) -> Manifest:
    manifest = Manifest.model_validate_json(path.read_text())
    for case in manifest.cases:
        task = (path.parent / case.task).resolve()
        if path.resolve().is_relative_to(task):
            raise ValueError("Benchmark labels cannot reside inside a task")
        if task_identity(task) != case.bundle_hash or inventory(task) != case.files:
            raise ValueError(f"Case content changed: {case.case_id}")
        if case.defect:
            loc = case.defect.location
            target = (task / loc.path).resolve()
            if not target.is_relative_to(task) or not target.is_file():
                raise ValueError("Expected defect location is outside the case")
            if not loc.quote.strip() or " ".join(loc.quote.split()) not in " ".join(
                target.read_text().split()
            ):
                raise ValueError("Expected defect location is not grounded")
    return manifest
