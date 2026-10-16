"""Granular findings with a mechanically derived legacy compatibility view."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from repo2rlenv.quality.loop.models import (
    Assessment,
    Citation,
    Issue,
    ReadRequest,
    Record,
    Review,
    RolloutOutcome,
    SemanticProbe,
)

CriterionID = Literal[
    "alignment", "feasibility", "reference", "coverage", "alternatives", "gaming", "leakage"
]
CRITERIA = {
    "alignment": "task",
    "feasibility": "task",
    "reference": "verifier",
    "coverage": "verifier",
    "alternatives": "verifier",
    "gaming": "verifier",
    "leakage": "leakage",
}


class Criterion(Assessment):
    criterion: CriterionID


class Finding(Issue):
    criterion: CriterionID
    attribution: Literal["task", "solver", "infrastructure", "mixed", "unresolved"]
    confidence: Literal["low", "medium", "high"]
    consequence: str = Field(min_length=1, max_length=2000)
    basis: Literal["static", "observed", "derived"]


class Investigation(Record):
    hypothesis: str = Field(min_length=1, max_length=2000)
    outcome: Literal["supported", "dismissed", "unresolved"]
    explanation: str = Field(min_length=1, max_length=2000)
    evidence: list[Citation] = Field(max_length=8)


class EvidenceReview(Record):
    summary: str
    criteria: list[Criterion] = Field(min_length=7, max_length=7)
    rollout: RolloutOutcome
    issues: list[Finding] = Field(max_length=12)
    investigations: list[Investigation] = Field(max_length=12)
    probes: list[SemanticProbe] = Field(max_length=4)
    read_requests: list[ReadRequest] = Field(max_length=6)

    @model_validator(mode="after")
    def consistent_findings(self):
        assessments = {item.criterion: item for item in self.criteria}
        if set(assessments) != set(CRITERIA):
            raise ValueError("Assess every criterion exactly once")
        for criterion, assessment in assessments.items():
            blocking = [
                issue
                for issue in self.issues
                if issue.criterion == criterion and issue.severity == "blocking"
            ]
            if assessment.status == "fail" and not blocking:
                raise ValueError(f"Failed criterion {criterion} needs a blocking finding")
            if assessment.status != "fail" and blocking:
                raise ValueError(f"Blocking findings require criterion {criterion} to fail")
        for issue in self.issues:
            if issue.severity == "blocking" and (
                issue.attribution in {"solver", "unresolved"}
                or (issue.attribution == "infrastructure" and issue.category != "probe")
            ):
                raise ValueError("Solver or external infrastructure failure is not a task defect")
        for item in self.investigations:
            if item.outcome != "unresolved" and not item.evidence:
                raise ValueError("Concluded investigations need evidence")
        return self

    def legacy(self) -> Review:
        """Existing execution/repair code receives no new model-selected verdict."""
        aggregates = {}
        for group in ("task", "verifier", "leakage"):
            items = [c for c in self.criteria if CRITERIA[c.criterion] == group]
            statuses = {c.status for c in items}
            status = (
                "fail" if "fail" in statuses else "unknown" if "unknown" in statuses else "pass"
            )
            aggregates[group] = Assessment(
                status=status,
                score=min(c.score for c in items),
                explanation="; ".join(f"{c.criterion}: {c.explanation}" for c in items),
                evidence=[citation for c in items for citation in c.evidence],
            )
        return Review(
            summary=self.summary,
            **aggregates,
            rollout=self.rollout,
            issues=[Issue(**i.model_dump(include=set(Issue.model_fields))) for i in self.issues],
            probes=self.probes,
            read_requests=self.read_requests,
        )

    def validate_evidence(self, context, trials) -> None:
        citations = [
            c
            for item in [*self.criteria, *self.issues, *self.investigations]
            for c in item.evidence
        ]
        if any(not c.quote.strip() for c in citations):
            raise ValueError("Evidence excerpts cannot be blank")
        context.validate_review(self.legacy())
        # Hypotheses are not in the legacy view, but must be grounded too.
        for item in self.investigations:
            for citation in item.evidence:
                text = context.documents.get(citation.path, "")
                if " ".join(citation.quote.split()) not in " ".join(text.split()):
                    raise ValueError(f"Ungrounded investigation citation: {citation.path}")
        observed = {
            f"evidence/{index}-{trial.role}/"
            for index, trial in enumerate(trials)
            if trial.reward is not None and trial.exception_type in {None, "AgentTimeoutError"}
        }
        for issue in self.issues:
            if issue.basis == "observed" and not any(
                citation.path.startswith(tuple(observed)) for citation in issue.evidence
            ):
                raise ValueError("Observed findings require a completed bound trial citation")
