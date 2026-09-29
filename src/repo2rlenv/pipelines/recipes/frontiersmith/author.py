"""Durable, bounded OpenAI Responses calls using the shared campaign ledger."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from repo2rlenv.auth import resolve_llm_api_key
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.tasksmith.author.openai_agent import PRICES, model_name, usage_cost


class InvalidArtifact(ValueError):
    """A received, metered response did not satisfy the stage contract."""


def author(spec, schema, *, prompt, payload, path: Path, ledger, operation, max_tokens, resume):
    name = model_name(spec.qualified_name)
    if spec.provider != "openai" or spec.endpoint or spec.fallback:
        raise ValueError("FrontierSmith requires direct OpenAI without implicit fallbacks")
    request = {
        "model": name,
        "instructions": prompt,
        "input": json.dumps(payload, sort_keys=True),
        "reasoning": {"effort": "medium"},
        "max_output_tokens": max_tokens,
        "store": False,
        "service_tier": "default",
    }
    identity = hashlib.sha256(
        json.dumps({**request, "schema": schema.model_json_schema()}, sort_keys=True).encode()
    ).hexdigest()
    if path.exists():
        stored = json.loads(path.read_text())
        if not resume or stored["identity"] != identity or stored["operation"] != operation:
            raise ValueError("Model receipt exists for a different request or resume is disabled")
        if stored["ledger"] != str(ledger.path.resolve()):
            raise ValueError("Reconcile the existing model operation before another dispatch")
        if stored["state"] == "invalid_output":
            ledger.settle(operation, stored["cost_usd"], evidence=str(path.resolve()))
            raise InvalidArtifact(stored["error"])
        if stored["state"] != "completed":
            raise ValueError("Reconcile the existing model operation before another dispatch")
        ledger.settle(operation, stored["cost_usd"], evidence=str(path.resolve()))
        return schema.model_validate(stored["artifact"])

    from openai import APIStatusError, OpenAI

    key = resolve_llm_api_key(spec.provider, spec.api_key_env)
    if not key:
        raise ValueError("OpenAI API key is required")
    input_bound = len(json.dumps({**request, "schema": schema.model_json_schema()}).encode()) + 4096
    if input_bound > 240000:
        raise ValueError("Generation context exceeds the bounded pricing contract")
    reservation = (input_bound * PRICES[name][3] + max_tokens * PRICES[name][2]) / 1000000
    ledger.reserve(operation, reservation, f"FrontierSmith {schema.__name__}: {name}")
    record = {
        "identity": identity,
        "operation": operation,
        "state": "dispatched",
        "ledger": str(ledger.path.resolve()),
    }
    save_record(
        path.with_suffix(".request.json"), {**request, "schema": schema.model_json_schema()}
    )
    save_record(path, record)
    try:
        with OpenAI(api_key=key, max_retries=0, timeout=spec.timeout_sec) as client:
            response = client.responses.create(
                **request,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema.__name__,
                        "strict": True,
                        "schema": schema.model_json_schema(),
                    }
                },
            )
        data = response.model_dump(mode="json", warnings=False)
        save_record(path.with_suffix(".response.json"), data)
        cost = usage_cost(name, data["usage"])
        try:
            if response.status != "completed":
                raise ValueError("Model output was incomplete")
            artifact = schema.model_validate_json(response.output_text)
        except (ValueError, SyntaxError) as exc:
            record.update(state="invalid_output", cost_usd=str(cost), error=str(exc)[:4000])
            save_record(path, record)
            ledger.settle(operation, cost, evidence=str(path.resolve()))
            raise InvalidArtifact(record["error"]) from exc
        record.update(state="completed", artifact=artifact.model_dump(), cost_usd=str(cost))
        save_record(path, record)
        ledger.settle(operation, cost, evidence=str(path.resolve()))
        return artifact
    except APIStatusError as exc:
        record.update(
            state="provider_error", status_code=exc.status_code, request_id=exc.request_id
        )
        save_record(path, record)
        if exc.status_code in {400, 401, 403, 404, 422}:
            ledger.settle(operation, 0, evidence=str(path.resolve()))
        else:
            ledger.mark_uncertain(operation, str(path.resolve()))
        raise
    except BaseException as exc:
        op = next(op for op in ledger.status()["operations"] if op["id"] == operation)
        if op["status"] != "settled":
            if record["state"] == "dispatched":
                record.update(state="uncertain", exception_type=type(exc).__name__)
                save_record(path, record)
            ledger.mark_uncertain(operation, str(path.resolve()))
        raise
