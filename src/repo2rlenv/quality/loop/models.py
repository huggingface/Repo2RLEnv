"""Evidence and bounded decisions for the practical-generation review profile."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from repo2rlenv.spec.input import LLMSpec


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Citation(Record):
    path: str
    quote: str = Field(min_length=1, max_length=1000)


class Assessment(Record):
    status: Literal["pass", "fail", "unknown"]
    score: int = Field(ge=0, le=4)
    explanation: str
    evidence: list[Citation] = Field(min_length=1)


class Issue(Record):
    category: Literal["instruction", "verifier", "leakage", "reference", "packaging", "probe"]
    severity: Literal["blocking", "improvement"]
    problem: str
    repair: str
    evidence: list[Citation] = Field(min_length=1)


class ReadRequest(Record):
    path: str
    query: str | None
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def bounded_range(self):
        if not 0 <= self.end_line - self.start_line < 400:
            raise ValueError("Read at most 400 lines in forward order")
        return self


class SemanticProbe(Record):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,35}$")
    kind: Literal["wrong_solution", "valid_alternative"]
    focus: Literal["general", "lazy_output", "numeric_tolerance"] = "general"
    rationale: str = Field(min_length=1)
    evidence: list[Citation] = Field(min_length=1)
    # Run after the reference in a private probe variant, never on the controller.
    script: str = Field(min_length=1, max_length=16000)


class ProbeManifest(Record):
    bundle_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    probes: list[SemanticProbe] = Field(min_length=1, max_length=4)


class Review(Record):
    summary: str
    task: Assessment
    verifier: Assessment
    leakage: Assessment
    rollout: Literal[
        "not_run",
        "legitimate_success",
        "legitimate_failure",
        "reward_hack",
        "task_defect",
        "infrastructure_failure",
        "incomplete",
        "insufficient_evidence",
    ]
    issues: list[Issue] = Field(max_length=12)
    probes: list[SemanticProbe] = Field(max_length=4)
    read_requests: list[ReadRequest] = Field(max_length=6)

    @property
    def sound(self) -> bool:
        return (
            not self.read_requests
            and all(
                getattr(self, name).status == "pass" for name in ("task", "verifier", "leakage")
            )
            and not any(issue.severity == "blocking" for issue in self.issues)
            and self.rollout not in {"reward_hack", "task_defect", "insufficient_evidence"}
        )


class Edit(Record):
    path: str
    old: str
    new: str
    executable: bool


class Repair(Record):
    explanation: str = Field(max_length=1500)
    addressed_issues: list[str] = Field(min_length=1)
    edits: list[Edit] = Field(max_length=16)
    probe_replacements: list[SemanticProbe] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def targeted_changes(self):
        if not self.edits and not self.probe_replacements:
            raise ValueError("A repair must change task files or an invalid alternative probe")
        if any(probe.kind != "valid_alternative" for probe in self.probe_replacements):
            raise ValueError("Previously demonstrated wrong-solution probes cannot be replaced")
        return self


class LoopOptions(Record):
    review_model: LLMSpec = Field(
        default_factory=lambda: LLMSpec(provider="anthropic", model="claude-sonnet-4-6")
    )
    repair_model: LLMSpec | None = None
    escalation_model: LLMSpec | None = None
    solver_model: LLMSpec = Field(
        default_factory=lambda: LLMSpec(provider="anthropic", model="claude-sonnet-4-6")
    )
    repair: bool = False
    run_rollout: bool = False
    max_repairs: int = Field(default=2, ge=0, le=5)
    max_read_rounds: int = Field(default=2, ge=0, le=4)
    max_probes: int = Field(default=2, ge=0, le=4)
    context_chars: int = Field(default=100000, ge=16000, le=250000)
    model_tokens: int = Field(default=6000, ge=1024, le=16000)
    max_turns: int = Field(default=24, ge=1, le=100)
    solver_tokens: int = Field(default=4096, ge=256, le=8192)
    trial_timeout_sec: int = Field(default=900, ge=30, le=3600)
    success_reward: float = Field(default=1.0, allow_inf_nan=False)
    model_reservation_usd: str = "1.00"
    solver_reservation_usd: str = "4.00"
    max_spend_usd: str = "15.00"

    @model_validator(mode="after")
    def explicit_routes_and_limits(self):
        from decimal import Decimal

        for name in ("model_reservation_usd", "solver_reservation_usd", "max_spend_usd"):
            amount = Decimal(getattr(self, name))
            if not amount.is_finite() or amount <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for spec in (
            self.review_model,
            self.repair_model,
            self.escalation_model,
            self.solver_model,
        ):
            if spec and (spec.provider not in {"openai", "anthropic"} or spec.fallback):
                raise ValueError(
                    "Use explicit OpenAI/Anthropic routes; escalation is metered separately"
                )
        return self


class TrialRecord(Record):
    role: Literal["baseline", "oracle", "rollout", "probe"]
    bundle_hash: str
    result: str
    result_sha256: str
    agent: str
    model: str | None
    reward: float | None = Field(allow_inf_nan=False, strict=True)
    exception_type: str | None
    # Imported, checksum-bound evidence is valid but not cryptographically attested.
    binding: Literal["receipt", "harbor_checksum"]
    agent_exit_code: int | None = None
    probe_installed: bool | None = None
    probe: SemanticProbe | None = None


class LoopResult(Record):
    schema_version: Literal["1"] = "1"
    profile: Literal["practical-generation-v1"] = "practical-generation-v1"
    status: Literal["usable", "reviewed", "needs_repair", "needs_evidence", "budget_exhausted"]
    source_hash: str
    bundle_hash: str
    task_path: str
    repairs: int
    review: Review | None
    trials: list[TrialRecord]
    reasons: list[str]
    accounted_usd: str
    reserved_usd: str
