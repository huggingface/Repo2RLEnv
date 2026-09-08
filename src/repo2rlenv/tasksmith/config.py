from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from repo2rlenv.curation.budget import Budget


class TasksmithConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: int = 1
    provider: Literal["modal", "daytona"] = "modal"
    author_runtime: Literal["pi", "opencode"] = "pi"
    author_model: str = "anthropic/claude-sonnet-4-6"
    reviewer_model: str = "anthropic/claude-opus-4-6"
    solver_models: list[str] = Field(
        default_factory=lambda: ["anthropic/claude-sonnet-4-6", "anthropic/claude-opus-4-6"],
        min_length=2,
        max_length=2,
    )
    ledger_path: Path
    ledger_limit_usd: float = Field(gt=0, le=500)
    campaign_id: str = Field(pattern=r"^[a-zA-Z0-9_.:-]+$")
    campaign_limit_usd: float = Field(default=110, gt=0, le=500)
    candidate_limit_usd: float = Field(default=20, gt=0)
    author_stage_limit_usd: float = Field(default=6, gt=0)
    review_stage_limit_usd: float = Field(default=3, gt=0)
    solver_limit_usd: float = Field(default=3, gt=0)
    author_turns: int = Field(default=45, ge=1, le=100)
    solver_turns: int = Field(default=30, ge=1, le=100)
    max_revisions: int = Field(default=3, ge=1, le=6)
    candidate_timeout_sec: int = Field(default=14400, ge=600, le=43200)
    trial_timeout_sec: int = Field(default=900, ge=60, le=1800)
    workspace_timeout_sec: int = Field(default=3600, ge=300, le=7200)
    cloud_reservation_usd: float = Field(default=2, ge=0.1)
    validation_reserve_usd: float = Field(default=8, ge=1)
    concurrency: int = Field(default=1, ge=1, le=3)

    @model_validator(mode="after")
    def constraints(self):
        if len(set(self.solver_models)) != len(self.solver_models):
            raise ValueError("Solver models must be distinct")
        if self.validation_reserve_usd >= self.candidate_limit_usd:
            raise ValueError("Candidate allocation must leave room for construction")
        return self

    def budget(self, logical_id: str | None = None) -> Budget:
        return Budget(
            self.ledger_path,
            self.ledger_limit_usd,
            scope=f"{self.campaign_id}:{logical_id}" if logical_id else None,
            scope_limit=self.candidate_limit_usd if logical_id else None,
            group=self.campaign_id,
            group_limit=self.campaign_limit_usd,
        )

    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def spend_summary(self) -> dict:
        with self.budget()._locked() as state:
            entries = list(state["entries"].values())
            campaign = [e for e in entries if e.get("group") == self.campaign_id]
            return {
                "campaign_charged_or_reserved_usd": sum(e["charged_usd"] for e in campaign),
                "campaign_metered_usd": sum(
                    e["charged_usd"] for e in campaign if e["status"] == "metered"
                ),
                "campaign_estimated_usd": sum(
                    e["charged_usd"] for e in campaign if e["status"] == "estimated"
                ),
                "campaign_reserved_usd": sum(
                    e["charged_usd"] for e in campaign if e["status"] == "reserved"
                ),
                "primary_ledger_charged_or_reserved_usd": sum(e["charged_usd"] for e in entries),
            }


def reconcile_ledgers(paths: list[Path], primary: Path, ceiling: float = 500) -> dict:
    """Read retained ledgers without settling uncertain entries or duplicating charges."""
    if not math.isfinite(ceiling) or ceiling <= 0:
        raise ValueError("Invalid total ceiling")
    combined: dict[str, dict] = {}
    primary_ids: set[str] = set()
    identities = []
    for path in dict.fromkeys(p.resolve() for p in paths):
        raw = path.read_bytes()
        entries = json.loads(raw)["entries"]
        if not isinstance(entries, dict):
            raise ValueError("Invalid ledger entries")
        for key, entry in entries.items():
            amount = entry["charged_usd"]
            if not math.isfinite(amount) or amount < 0:
                raise ValueError("Invalid ledger charge")
            if key in combined and combined[key] != entry:
                raise ValueError(f"Conflicting duplicate ledger entry: {key}")
            combined[key] = entry
        if path == primary.resolve():
            primary_ids = set(entries)
        identities.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()})
    if not any(Path(p["path"]) == primary.resolve() for p in identities):
        raise ValueError("Primary ledger must be included")
    external = sum(e["charged_usd"] for key, e in combined.items() if key not in primary_ids)
    total = sum(e["charged_usd"] for e in combined.values())
    return {
        "ledger_identities": identities,
        "ceiling_usd": ceiling,
        "charged_or_reserved_usd": total,
        "unresolved_reserved_usd": sum(
            e["charged_usd"] for e in combined.values() if e["status"] == "reserved"
        ),
        "external_commitments_usd": external,
        "primary_ledger_limit_usd": max(0, ceiling - external),
        "remaining_usd": max(0, ceiling - total),
    }
