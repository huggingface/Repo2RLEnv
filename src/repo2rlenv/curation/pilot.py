"""Frozen five-source experiment sharing the existing production cost ledger."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

from repo2rlenv.curation.artifacts import release_task
from repo2rlenv.curation.budget import Budget, BudgetExceeded
from repo2rlenv.curation.campaign import (
    CandidateDeferred,
    _validate_accepted,
    campaign_lock,
    curate_one,
    save,
)
from repo2rlenv.curation.models import CampaignConfig
from repo2rlenv.curation.protocol import DraftLimitExceeded
from repo2rlenv.curation.sources import PR_PATTERN


def runtime_digest() -> str:
    h = hashlib.sha256()
    for path in sorted(Path(__file__).parent.rglob("*.py")):
        h.update(path.relative_to(Path(__file__).parent).as_posix().encode() + b"\0")
        h.update(path.read_bytes())
    return h.hexdigest()


def validate_protocol(protocol: dict) -> CampaignConfig:
    config = CampaignConfig.model_validate(protocol["config"])
    sources = protocol["sources"]
    if len(sources) != 5 or len({s["url"] for s in sources}) != 5:
        raise ValueError("Pilot requires exactly five distinct pinned sources")
    for source in sources:
        match = PR_PATTERN.fullmatch(source["url"])
        if not match:
            raise ValueError("Invalid source URL")
        repo, number = match.groups()
        expected_id = repo.replace("/", "-").replace("_", "-").lower() + "-" + number
        if source["id"] != expected_id or source["repo"] != repo:
            raise ValueError("Source identity mismatch")
        if not all(re.fullmatch(r"[0-9a-f]{40}", source[k]) for k in ("base_sha", "head_sha")):
            raise ValueError("Sources must have pinned base and head commits")
    if (
        config.target != 5
        or config.budget_usd != 40
        or config.max_candidate_usd != 8
        or config.max_revisions != 2
        or config.max_candidate_drafts != 2
        or not config.require_verification_plan
        or not config.specification_review
        or not config.verifier_review
    ):
        raise ValueError("Pilot requires five slots, $40/$8 caps, two drafts and all reviews")
    if not re.fullmatch(r"[a-z0-9-]+", protocol["id"]):
        raise ValueError("Invalid pilot ID")
    if not 0 < protocol["production_limit_usd"] <= 380:
        raise ValueError("Production ledger limit exceeds the retained allocation")
    if protocol["runtime_digest"] != runtime_digest():
        raise ValueError("Frozen runtime changed; refuse to mix policies within this pilot")
    return config


async def run_pilot(protocol_path: Path, out: Path) -> dict:
    protocol = json.loads(protocol_path.read_text())
    config = validate_protocol(protocol)
    out = out.resolve()
    ledger = Path(protocol["ledger"]).resolve()
    if not ledger.is_file():
        raise ValueError("Pilot must use the retained production ledger")
    group = "systematic-pilot:" + protocol["id"]
    with campaign_lock(out):
        claim = {
            "out": str(out),
            "protocol_digest": hashlib.sha256(
                json.dumps(protocol, sort_keys=True).encode()
            ).hexdigest(),
        }
        # An output-local lock alone cannot prevent two different directories
        # launching the same experiment before either has incurred a charge.
        with Budget(ledger, protocol["production_limit_usd"])._locked() as shared:
            claims = shared.setdefault("pilot_claims", {})
            if group in claims and claims[group] != claim:
                raise ValueError("Pilot is bound to its original output directory and protocol")
            claims[group] = claim
        frozen = out / "protocol.json"
        state = json.loads(ledger.read_text())
        if not frozen.exists() and any(e.get("group") == group for e in state["entries"].values()):
            raise ValueError("Pilot already has costs; restore its original output directory")
        if frozen.exists() and json.loads(frozen.read_text()) != protocol:
            raise ValueError("Cannot alter a frozen pilot")
        save(frozen, protocol)
        save(out / "config.json", config.model_dump())
        manifest_path = out / "manifest.json"
        if not manifest_path.exists() and any(
            e.get("group") == group for e in state["entries"].values()
        ):
            raise ValueError("Missing pilot manifest with retained costs; refuse restart")
        manifest = (
            json.loads(manifest_path.read_text())
            if manifest_path.exists()
            else {
                "status": "running",
                "seeds": [s["url"] for s in protocol["sources"]],
                "rows": [],
                "shared_ledger": str(ledger),
                "budget_group": group,
                "budget_usd": 40,
                "human_review": "pending",
                "manual_interventions": [],
            }
        )
        # Never silently restart an author after a crash. The slot remains in the
        # denominator; outstanding reservations remain charged in the shared ledger.
        for row in manifest["rows"]:
            if row["status"] == "running":
                row.update(
                    status="interrupted", reasons=["Controller interrupted; no automatic rerun"]
                )
        save(manifest_path, manifest)
        done = {r["source"] for r in manifest["rows"]}
        semaphore = asyncio.Semaphore(config.concurrency)

        async def process(source: dict) -> None:
            if source["url"] in done:
                return
            async with semaphore:
                scope = group + ":" + source["url"]
                budget = Budget(
                    ledger,
                    protocol["production_limit_usd"],
                    scope=scope,
                    scope_limit=8,
                    group=group,
                    group_limit=40,
                )
                root = out / "candidates" / source["id"] / str(time.time_ns())
                root.mkdir(parents=True)
                row = {
                    "source": source["url"],
                    "id": source["id"],
                    "status": "running",
                    "budget_scope": scope,
                    "candidate_path": str(root),
                }
                manifest["rows"].append(row)
                save(manifest_path, manifest)
                try:
                    result = await curate_one(source, root, config, budget)
                    if result["status"] == "accepted":
                        task = _validate_accepted(out, result, config)
                        release_task(task, out / "tasks" / source["id"])
                except (CandidateDeferred, DraftLimitExceeded, BudgetExceeded) as exc:
                    status = {
                        CandidateDeferred: "deferred",
                        DraftLimitExceeded: "repair_limit",
                        BudgetExceeded: "budget_exhausted",
                    }[type(exc)]
                    result = {"status": status, "reasons": [str(exc)]}
                except Exception as exc:
                    result = {
                        "status": "execution_failure",
                        "reasons": [f"{type(exc).__name__}: {exc}"],
                    }
                row.update(result, charged_or_reserved_usd=budget.spent)
                save(root / "pilot-result.json", row)
                save(manifest_path, manifest)

        async with asyncio.TaskGroup() as tasks:
            for source in protocol["sources"]:
                tasks.create_task(process(source))
        manifest.update(
            status="complete",
            automatic_accepted=sum(r["status"] == "accepted" for r in manifest["rows"]),
            charged_or_reserved_usd=sum(
                r.get("charged_or_reserved_usd", 0) for r in manifest["rows"]
            ),
            scale_decision="pending_independent_audit",
        )
        # Cost includes crashed slots and outstanding reservations, not only results.
        state = json.loads(ledger.read_text())
        manifest["charged_or_reserved_usd"] = sum(
            e["charged_usd"] for e in state["entries"].values() if e.get("group") == group
        )
        save(manifest_path, manifest)
        return manifest
