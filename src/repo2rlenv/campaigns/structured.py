"""Correct completed, invalid structured responses without replaying provider effects."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from repo2rlenv.campaigns.llm import metered_complete
from repo2rlenv.execution.lifecycle import save_record


def validated_complete[Result: BaseModel](
    schema: type[Result],
    model,
    *,
    ledger,
    receipt: Path,
    operation_id: str,
    system: str,
    payload: dict,
    reservation_usd: str,
    max_tokens: int,
    resume: bool,
    max_attempts: int = 3,
    validate: Callable[[Result], None] | None = None,
) -> Result:
    """Only schema/content errors from a completed response authorize correction."""
    if not 1 <= max_attempts <= 3:
        raise ValueError("Structured correction supports one to three attempts")
    feedback = None
    for attempt in range(max_attempts):
        path = (
            receipt if attempt == 0 else receipt.with_name(f"{receipt.stem}-repair-{attempt}.json")
        )
        response = metered_complete(
            model,
            ledger=ledger,
            receipt=path,
            operation_id=operation_id if attempt == 0 else f"{operation_id}:repair:{attempt}",
            reservation_usd=reservation_usd,
            max_tokens=max_tokens,
            resume=resume,
            system=system,
            user=json.dumps(payload if feedback is None else {"input": payload, **feedback}),
            response_schema=schema.model_json_schema(),
        )
        try:
            result = schema.model_validate_json(response.content)
            if validate is not None:
                validate(result)
            return result
        except (ValueError, SyntaxError) as exc:
            feedback = {
                "validation_error": str(exc)[:4000],
                "previous_response": response.content,
                "instruction": "Correct this completed response, preserving its task and behavior. Return complete valid JSON/code. There are at most two correction attempts; fix the specific error rather than redesigning the task.",
            }
            save_record(
                path.with_name(path.stem + "-validation.json"),
                {"error": str(exc), "attempt": attempt + 1},
            )
    raise ValueError(
        f"Structured response exhausted {max_attempts} attempts: {feedback['validation_error']}"
    )
