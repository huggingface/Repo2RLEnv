"""Transactional spend reservations shared by all operations in a campaign.

Amounts are stored as integer micro-dollars. Uncertain provider outcomes retain
their reservation until explicitly reconciled; recorded actual cost may exceed a
reservation, but that overrun must prevent future dispatch rather than disappear.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

_SCALE = Decimal(1_000_000)


class BudgetExceeded(RuntimeError):
    """The proposed operation would exceed the remaining allowance."""

    def __init__(self, message: str, *, denials: tuple[BudgetDenial, ...] = ()):
        super().__init__(message)
        self.denials = denials


@dataclass(frozen=True)
class BudgetDenial:
    """Capacity evidence from the same transaction that refused reservation."""

    scope: str | None  # None is the global campaign cap.
    requested_micros: int
    limit_micros: int
    accounted_micros: int
    reserved_micros: int
    uncertain_micros: int

    @property
    def remaining_micros(self) -> int:
        return self.limit_micros - self.accounted_micros - self.reserved_micros

    @property
    def reason(self) -> str:
        if self.requested_micros > self.limit_micros:
            return "request_exceeds_limit"
        if self.requested_micros + self.accounted_micros > self.limit_micros:
            return "accounted_exhaustion"
        if (
            self.requested_micros + self.accounted_micros + self.uncertain_micros
            > self.limit_micros
        ):
            return "uncertain_reservations"
        return "reservation_pressure"

    def as_dict(self) -> dict:
        return {
            "scope": self.scope,
            "reason": self.reason,
            "requested_usd": _usd(self.requested_micros),
            "limit_usd": _usd(self.limit_micros),
            "accounted_usd": _usd(self.accounted_micros),
            "reserved_usd": _usd(self.reserved_micros),
            "uncertain_usd": _usd(self.uncertain_micros),
            "remaining_usd": _usd(self.remaining_micros),
        }


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
        self,
        operation_id: str,
        amount_usd: Decimal | str | float,
        description: str,
        *,
        scopes: tuple[tuple[str, Decimal | str | float], ...] = (),
    ) -> None:
        if not operation_id.strip() or not description.strip():
            raise ValueError("Reservation requires an operation ID and description")
        amount = _micros(amount_usd)
        if amount == 0:
            raise ValueError("Paid operations require a positive reservation")
        limits = []
        for prefix, limit in scopes:
            if not prefix or prefix not in operation_id:
                raise ValueError("Operation must belong to each budget scope")
            limits.append((prefix, _micros(limit)))
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM operations WHERE id=?", (operation_id,)).fetchone():
                raise OperationAlreadyRecorded(operation_id)
            limit = db.execute("SELECT limit_micros FROM budget WHERE id=1").fetchone()[0]
            denials = []
            # Inspect every cap atomically: a shared hold must not mask an exhausted
            # candidate, quality or global allowance that cannot be waited away.
            for prefix, scoped_limit in [(None, limit), *limits]:
                spent, reserved, uncertain = db.execute(
                    "SELECT COALESCE(SUM(actual_micros),0), "
                    "COALESCE(SUM(CASE WHEN status != 'settled' THEN reserved_micros ELSE 0 END),0), "
                    "COALESCE(SUM(CASE WHEN status = 'uncertain' THEN reserved_micros ELSE 0 END),0) "
                    "FROM operations WHERE ? IS NULL OR instr(id, ?) > 0",
                    (prefix, prefix),
                ).fetchone()
                if spent + reserved + amount > scoped_limit:
                    denials.append(
                        BudgetDenial(prefix, amount, scoped_limit, spent, reserved, uncertain)
                    )
            if denials:
                messages = [
                    f"{'Budget scope ' + denial.scope if denial.scope else operation_id}: "
                    f"requested ${_usd(amount)}, remaining ${_usd(denial.remaining_micros)} "
                    f"({denial.reason})"
                    for denial in denials
                ]
                raise BudgetExceeded(
                    "; ".join(messages) + "; completed evidence retained", denials=tuple(denials)
                )
            db.execute(
                "INSERT INTO operations(id,description,reserved_micros,status) VALUES (?,?,?,'reserved')",
                (operation_id, description, amount),
            )

    def increase_limit(self, new_limit_usd, *, expected_limit_usd, evidence: str) -> None:
        """Record an explicitly authorized increase without changing operations."""
        new_limit, expected = _micros(new_limit_usd), _micros(expected_limit_usd)
        if new_limit <= expected or not evidence.strip():
            raise ValueError("A budget increase requires a higher limit and authorization evidence")
        with self._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS budget_increases ("
                "evidence TEXT PRIMARY KEY, previous_micros INTEGER NOT NULL, "
                "new_micros INTEGER NOT NULL, recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            prior = db.execute(
                "SELECT previous_micros,new_micros FROM budget_increases WHERE evidence=?",
                (evidence,),
            ).fetchone()
            if prior is not None:
                if tuple(prior) == (expected, new_limit):
                    return
                raise ValueError("Authorization evidence already records a different increase")
            current = db.execute("SELECT limit_micros FROM budget WHERE id=1").fetchone()[0]
            if current != expected:
                raise ValueError(
                    "Budget limit changed; inspect it before authorizing another increase"
                )
            db.execute("UPDATE budget SET limit_micros=? WHERE id=1", (new_limit,))
            db.execute(
                "INSERT INTO budget_increases(evidence,previous_micros,new_micros) VALUES (?,?,?)",
                (evidence, expected, new_limit),
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
