"""Small, locked campaign ledger. Uncertain usage remains reserved."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

DEFAULT = Path(__file__).resolve().parents[1] / "runs" / "budget.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(path.read_text()) if path.exists() else {
            "limit_usd": "500.00",
            "authorization": "2026-09-10: Use a new $500 budget for these reproductions",
            "entries": {},
        }
        yield data
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(temporary, path)


def total(data: dict) -> Decimal:
    return sum((Decimal(e["accounted_usd"]) for e in data["entries"].values()), Decimal(0))


def reserve(key: str, amount: str, purpose: str, path: Path = DEFAULT) -> dict:
    value = Decimal(amount)
    if not value.is_finite() or value <= 0:
        raise ValueError("Reservation must be finite and positive")
    with locked(path) as data:
        if key in data["entries"]:
            raise ValueError(f"Operation already exists: {key}; inspect before retrying")
        if total(data) + value > Decimal(data["limit_usd"]):
            raise ValueError("Campaign allowance exceeded")
        entry = {"purpose": purpose, "reserved_usd": str(value), "accounted_usd": str(value),
                 "status": "reserved", "created_at": now()}
        data["entries"][key] = entry
    return entry


def settle(key: str, amount: str, evidence: str, kind: str, path: Path = DEFAULT) -> dict:
    value = Decimal(amount)
    if not value.is_finite() or value < 0 or not evidence:
        raise ValueError("Settlement requires nonnegative cost and evidence")
    if kind not in {"model_reported", "cloud_estimate", "provider_invoice", "not_started"}:
        raise ValueError("Unknown cost evidence kind")
    with locked(path) as data:
        entry = data["entries"][key]
        entry.update(accounted_usd=str(value), status=kind, evidence=evidence, updated_at=now())
    return entry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "reserve", "settle"])
    parser.add_argument("--key")
    parser.add_argument("--usd")
    parser.add_argument("--note", default="")
    parser.add_argument("--kind", default="model_reported")
    args = parser.parse_args()
    if args.action == "reserve":
        print(json.dumps(reserve(args.key, args.usd, args.note), indent=2))
    elif args.action == "settle":
        print(json.dumps(settle(args.key, args.usd, args.note, args.kind), indent=2))
    else:
        with locked(DEFAULT) as data:
            print(json.dumps({**data, "accounted_usd": str(total(data)),
                              "remaining_usd": str(Decimal(data["limit_usd"]) - total(data))},
                             indent=2))


if __name__ == "__main__":
    main()
