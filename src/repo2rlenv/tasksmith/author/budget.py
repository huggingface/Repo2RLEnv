"""Adapt the isolated author bridge to the shared transactional campaign ledger."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.quality.loop.client import RunBudget


class AuthorBudget:
    def __init__(self, budget: RunBudget, directory: Path, stage: str):
        self.budget, self.directory, self.stage = budget, directory, stage
        self.turn = 0

    @property
    def spent(self) -> float:
        return float(sum(Decimal(value) for value in self.budget.totals().values()))

    def reserve(self, amount: float, description: str) -> str:
        self.turn += 1
        operation = f"{self.budget.prefix}:author:{self.stage}:{self.turn}"
        self.budget.reserve(operation, str(amount), description)
        return operation

    def settle(self, operation: str, amount: float) -> None:
        receipt = self.directory / (operation.rsplit(":", 1)[-1] + ".json")
        save_record(
            receipt,
            {
                "operation": operation,
                "cost_usd": amount,
                "source": "author provider response",
                "trace": str(self.directory.parent / "trace.jsonl"),
            },
        )
        self.budget.settle(operation, str(amount), evidence=str(receipt.resolve()))
