"""Transactional spend reservations shared by all operations in a campaign.

Amounts are stored as integer micro-dollars. Uncertain provider outcomes retain
their reservation until explicitly reconciled; recorded actual cost may exceed a
reservation, but that overrun must prevent future dispatch rather than disappear.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

_SCALE = Decimal(1_000_000)


class BudgetExceeded(RuntimeError):
    """The proposed operation would exceed the remaining allowance."""


class OperationAlreadyRecorded(RuntimeError):
    """The operation must be inspected instead of dispatched again."""


def _micros(value: Decimal | str | float) -> int:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Budget amounts must be finite and nonnegative")
    return int((amount * _SCALE).to_integral_value(rounding=ROUND_CEILING))


def _usd(value: int) -> str:
    return format(Decimal(value) / _SCALE, ".6f")


class BudgetLedger:
    def __init__(self, path: Path, *, limit_usd: Decimal | str | float | None = None):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY, limit_micros INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS operations ("
                "id TEXT PRIMARY KEY, description TEXT NOT NULL, "
                "reserved_micros INTEGER NOT NULL, actual_micros INTEGER, "
                "status TEXT NOT NULL CHECK(status IN ('reserved','uncertain','settled')), "
                "evidence TEXT NOT NULL DEFAULT '')"
            )
            row = db.execute("SELECT limit_micros FROM budget WHERE id=1").fetchone()
            if row is None:
                if limit_usd is None:
                    raise ValueError("A new ledger requires an explicit spending limit")
                db.execute("INSERT INTO budget VALUES (1, ?)", (_micros(limit_usd),))
            elif limit_usd is not None and row[0] != _micros(limit_usd):
                raise ValueError(
                    "Existing campaign limit differs; refusing an implicit budget reset"
                )

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _totals(db: sqlite3.Connection) -> tuple[int, int, int]:
        limit = db.execute("SELECT limit_micros FROM budget WHERE id=1").fetchone()[0]
        spent, reserved = db.execute(
            "SELECT COALESCE(SUM(actual_micros),0), "
            "COALESCE(SUM(CASE WHEN status != 'settled' THEN reserved_micros ELSE 0 END),0) "
            "FROM operations"
        ).fetchone()
        return limit, spent, reserved

    def reserve(
        self, operation_id: str, amount_usd: Decimal | str | float, description: str
    ) -> None:
        if not operation_id.strip() or not description.strip():
            raise ValueError("Reservation requires an operation ID and description")
        amount = _micros(amount_usd)
        if amount == 0:
            raise ValueError("Paid operations require a positive reservation")
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM operations WHERE id=?", (operation_id,)).fetchone():
                raise OperationAlreadyRecorded(operation_id)
            limit, spent, reserved = self._totals(db)
            if spent + reserved + amount > limit:
                raise BudgetExceeded(
                    f"{operation_id}: requested ${_usd(amount)}, remaining ${_usd(limit - spent - reserved)}"
                )
            db.execute(
                "INSERT INTO operations(id,description,reserved_micros,status) VALUES (?,?,?,'reserved')",
                (operation_id, description, amount),
            )

    def mark_uncertain(self, operation_id: str, evidence: str) -> None:
        if not evidence.strip():
            raise ValueError("An uncertain operation needs recovery evidence")
        with self._transaction() as db:
            row = db.execute("SELECT status FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None or row[0] == "settled":
                raise ValueError("Only an outstanding reservation can become uncertain")
            db.execute(
                "UPDATE operations SET status='uncertain', evidence=? WHERE id=?",
                (evidence, operation_id),
            )

    def settle(
        self, operation_id: str, actual_usd: Decimal | str | float, *, evidence: str
    ) -> None:
        amount = _micros(actual_usd)
        if not evidence.strip():
            raise ValueError("Settlement needs usage, billing or no-dispatch evidence")
        with self._transaction() as db:
            row = db.execute(
                "SELECT status,actual_micros,evidence FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"No reservation for {operation_id}")
            if row[0] == "settled":
                if row[1] == amount and row[2] == evidence:
                    return
                raise OperationAlreadyRecorded(f"Conflicting settlement for {operation_id}")
            db.execute(
                "UPDATE operations SET actual_micros=?, status='settled', evidence=? WHERE id=?",
                (amount, evidence, operation_id),
            )

    def status(self) -> dict:
        with self._transaction() as db:
            limit, spent, reserved = self._totals(db)
            operations = [dict(row) for row in db.execute("SELECT * FROM operations ORDER BY id")]
        return {
            "limit_usd": _usd(limit),
            "accounted_usd": _usd(spent),
            "reserved_usd": _usd(reserved),
            "remaining_usd": _usd(limit - spent - reserved),
            "operations": operations,
        }
