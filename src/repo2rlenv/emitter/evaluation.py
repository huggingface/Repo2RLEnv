"""Versioned, advisory evaluation labels shared by every Harbor emitter.

This namespace describes evidence; it is not part of the executable bundle
identity and is never sufficient on its own to authorize training acceptance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EvaluationStatus = Literal["unverified", "verified", "needs_repair", "blocked"]
EvaluationStage = Literal[
    "generation",
    "bootstrap",
    "construction",
    "review",
    "controls",
    "probes",
    "rollout",
    "repair",
    "complete",
    "unknown",
]
Provenance = Literal["unknown", "assisted", "unattended"]


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["quality_result", "baseline", "oracle", "probe", "rollout", "diagnosis"]
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject_bundle_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    # Harbor hashes the physical directory, including annotation bytes. Keep its
    # original checksum distinct from our annotation-independent bundle identity.
    harbor_task_checksum: str | None = None


class EvaluationLabel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    status: EvaluationStatus = "unverified"
    stage: EvaluationStage = "generation"
    reason_codes: list[str] = Field(default_factory=lambda: ["validation_not_run"])
    detail: str = "Generated task; validation has not been established."
    # No wall-clock value in the default: repeatable exports must keep matching
    # configuration bytes even before a check has been performed.
    checked_at: datetime | None = None
    provenance: Provenance = "unknown"
    subject_bundle_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    profile: str | None = None
    evidence: list[EvidenceReference] = Field(default_factory=list)
    source_task_path: str | None = None
    source_task_toml_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def meaningful_claim(self):
        import re

        if not self.reason_codes or any(
            not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) for code in self.reason_codes
        ):
            raise ValueError("Evaluation reason codes must be nonempty snake_case identifiers")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("Evaluation reason codes must be unique")
        if self.checked_at is not None and self.checked_at.utcoffset() is None:
            raise ValueError("Evaluation timestamps must include a timezone")
        if self.status == "verified":
            kinds = {item.kind for item in self.evidence}
            if (
                not self.subject_bundle_hash
                or not self.checked_at
                or not self.profile
                or not {"quality_result", "baseline", "oracle", "probe", "rollout"} <= kinds
            ):
                raise ValueError("Verified labels require a dated, evidence-bound quality result")
        return self

    def toml_metadata(self) -> dict:
        """TOML has no null type; absent evidence remains explicitly absent."""
        return self.model_dump(mode="json", exclude_none=True)


def generated_evaluation(
    *, provenance: Provenance = "unknown", subject_bundle_hash: str | None = None
) -> dict:
    return EvaluationLabel(
        provenance=provenance, subject_bundle_hash=subject_bundle_hash
    ).toml_metadata()


def evaluation_time() -> datetime:
    return datetime.now(UTC)
