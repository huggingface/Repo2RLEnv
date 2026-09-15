"""Meter the existing LLM client without silently retrying uncertain requests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import TypeAdapter

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.llm import LLMResponse, complete
from repo2rlenv.spec.input import LLMSpec


def metered_complete(
    spec: LLMSpec,
    *,
    ledger: BudgetLedger,
    operation_id: str,
    reservation_usd: str,
    receipt: Path,
    system: str,
    user: str,
    max_tokens: int,
    response_schema: dict | None = None,
    resume: bool = False,
) -> LLMResponse:
    if spec.fallback is not None:
        raise ValueError("Campaign fallbacks need their own reservation and receipt")
    request = {
        "system": system,
        "user": user,
        "max_tokens": max_tokens,
        "response_schema": response_schema,
    }
    request_hash = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    if receipt.exists():
        record = json.loads(receipt.read_text())
        if not resume or record.get("status") != "completed":
            raise FileExistsError(f"Reconcile existing model receipt: {receipt}")
        if (
            record.get("operation_id") != operation_id
            or record.get("model") != spec.qualified_name
            or record.get("request_sha256") != request_hash
            or record.get("ledger") != str(ledger.path.resolve())
        ):
            raise ValueError("Stored model response belongs to a different request")
        response = LLMResponse(**record["response"])
        operations = {item["id"]: item for item in ledger.status()["operations"]}
        if operation_id not in operations:
            raise ValueError("Stored response has no matching budget operation")
        if (
            operations[operation_id]["status"] != "settled"
            and response.cost_usd > 0
            and response.usage
        ):
            ledger.settle(operation_id, response.cost_usd, evidence=str(receipt.resolve()))
        return response
    ledger.reserve(operation_id, reservation_usd, f"LLM {spec.qualified_name}")
    try:
        save_record(receipt.with_suffix(".request.json"), request)
        save_record(
            receipt,
            {
                "status": "dispatched",
                "ledger": str(ledger.path.resolve()),
                "operation_id": operation_id,
                "model": spec.qualified_name,
                "request_sha256": request_hash,
            },
        )
        response = complete(
            spec, system=system, user=user, max_tokens=max_tokens, response_schema=response_schema
        )
        save_record(
            receipt,
            {
                "status": "completed",
                "ledger": str(ledger.path.resolve()),
                "operation_id": operation_id,
                "model": spec.qualified_name,
                "cost_basis": "litellm_estimate",
                "request_sha256": request_hash,
                "response": TypeAdapter(LLMResponse).dump_python(response, mode="json"),
            },
        )
        if response.cost_usd > 0 and response.usage:
            ledger.settle(operation_id, response.cost_usd, evidence=str(receipt.resolve()))
        else:
            ledger.mark_uncertain(
                operation_id, f"Missing usage or cost estimate: {receipt.resolve()}"
            )
        return response
    except BaseException:
        ledger.mark_uncertain(
            operation_id, f"Request or persistence interrupted; inspect {receipt.resolve()}"
        )
        raise
