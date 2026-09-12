"""Metered structured calls and per-run spending within the shared campaign."""

from __future__ import annotations

from decimal import Decimal
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel

from repo2rlenv.auth import resolve_llm_api_key
from repo2rlenv.campaigns.budget import BudgetExceeded, BudgetLedger
from repo2rlenv.campaigns.llm import metered_complete
from repo2rlenv.spec.input import LLMSpec


class RunBudget:
    """All effects still reserve on the campaign's transactional ledger."""

    def __init__(self, ledger: BudgetLedger, prefix: str, limit: str):
        self.ledger, self.prefix, self.limit = ledger, prefix, Decimal(limit)
        self.path = ledger.path

    def totals(self) -> dict[str, str]:
        operations = [op for op in self.ledger.status()["operations"] if self.prefix in op["id"]]
        spent = sum(op["actual_micros"] or 0 for op in operations)
        held = sum(op["reserved_micros"] for op in operations if op["status"] != "settled")
        return {
            "accounted_usd": str(Decimal(spent) / 1000000),
            "reserved_usd": str(Decimal(held) / 1000000),
        }

    def reserve(self, operation_id, amount_usd, description):
        if self.prefix not in operation_id:
            raise ValueError("Quality operation must belong to this run")
        totals = self.totals()
        used = sum(Decimal(value) for value in totals.values())
        if used + Decimal(str(amount_usd)) > self.limit:
            raise BudgetExceeded(
                "Quality run spending limit reached; completed evidence is retained"
            )
        self.ledger.reserve(operation_id, amount_usd, description)

    def status(self):
        return self.ledger.status()

    def settle(self, *args, **kwargs):
        return self.ledger.settle(*args, **kwargs)

    def mark_uncertain(self, *args, **kwargs):
        return self.ledger.mark_uncertain(*args, **kwargs)


def prompt(name: str) -> str:
    return files(__package__).joinpath("prompts", name + ".md").read_text()


class ModelRequestError(RuntimeError):
    """A provider effect needs diagnosis; never automatically redispatch it."""


def response_schema(model: type[BaseModel]) -> dict:
    """Require provider output fields even when readers accept legacy defaults."""
    schema = model.model_json_schema()

    def visit(value):
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object" and "properties" in value:
                value["required"] = list(value["properties"])
                value["additionalProperties"] = False
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    return schema


class JsonModel:
    def __init__(self, directory: Path, budget: RunBudget, *, reservation: str, max_tokens: int):
        self.directory, self.budget = directory, budget
        self.reservation, self.max_tokens = reservation, max_tokens

    def ask(self, schema: type[BaseModel], model: LLMSpec, system: str, user: str, key: str):
        receipt = self.directory / f"{key}.json"
        if not receipt.exists() and not resolve_llm_api_key(model.provider, model.api_key_env):
            raise ValueError(f"No API key for {model.provider}; no model request was dispatched")
        try:
            response = metered_complete(
                model,
                ledger=self.budget,
                operation_id=f"{self.budget.prefix}:model:{key}",
                reservation_usd=self.reservation,
                receipt=receipt,
                system=system,
                user=user,
                max_tokens=self.max_tokens,
                response_schema=response_schema(schema),
                resume=True,
            )
        except BudgetExceeded:
            raise
        except Exception as exc:
            raise ModelRequestError(
                f"{model.qualified_name} request failed ({type(exc).__name__}); inspect {receipt}"
            ) from None
        return schema.model_validate_json(response.content)
