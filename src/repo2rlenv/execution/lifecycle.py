"""Persist worker identity before dispatch and retain uncertain spend for recovery."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.execution.base import RemoteWorker, WorkerSpec, connect_worker, create_worker


def now() -> str:
    return datetime.now(UTC).isoformat()


def save_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def provision_worker(
    spec: WorkerSpec, directory: Path, ledger: BudgetLedger, *, reserve_usd: str
) -> tuple[RemoteWorker, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{spec.name}.json"
    # The stable name is also the provider recovery handle if a create response
    # is lost. Never blindly create another worker for this operation.
    with path.open("x") as handle:
        json.dump({"state": "claimed", "spec": spec.model_dump(), "claimed_at": now()}, handle)
    operation_id = f"worker:{spec.provider}:{spec.name}"
    record = {
        "state": "claimed",
        "spec": spec.model_dump(),
        "operation_id": operation_id,
        "ledger": str(ledger.path.resolve()),
        "claimed_at": now(),
    }
    try:
        ledger.reserve(operation_id, reserve_usd, f"{spec.provider} worker {spec.name}")
    except Exception:
        record["state"] = "reservation_failed"
        save_record(path, record)
        raise
    record.update(state="creating", started_at=now())
    save_record(path, record)
    try:
        worker = create_worker(spec)
        record.update(state="running", worker_id=worker.id, ready_at=now())
        save_record(path, record)
        return worker, path
    except BaseException:
        record["state"] = "creation_uncertain"
        save_record(path, record)
        ledger.mark_uncertain(operation_id, f"Inspect provider worker by name; receipt={path}")
        raise


def stop_worker(path: Path, ledger: BudgetLedger) -> dict:
    record = json.loads(path.read_text())
    if Path(record["ledger"]).resolve() != ledger.path.resolve():
        raise ValueError("Worker receipt belongs to another budget ledger")
    if record["state"] == "terminated":
        return record
    worker_id = record.get("worker_id")
    if worker_id is None:
        raise ValueError(
            "Worker creation is uncertain; resolve its provider identity before cleanup"
        )
    record["state"] = "terminating"
    save_record(path, record)
    try:
        connect_worker(record["spec"]["provider"], worker_id).terminate()
    except BaseException:
        record["state"] = "termination_uncertain"
        save_record(path, record)
        ledger.mark_uncertain(record["operation_id"], f"Termination not confirmed; receipt={path}")
        raise
    record.update(state="terminated", stopped_at=now())
    save_record(path, record)
    # Cleanup is confirmed, but its price must come from explicit evidence.
    ledger.mark_uncertain(
        record["operation_id"], f"Worker terminated; cost reconciliation pending; receipt={path}"
    )
    return record


def prepare_docker(worker: RemoteWorker) -> None:
    """Probe the real daemon; package presence alone is not build readiness."""
    script = """set -eu
mkdir -p /work /evidence
if ! docker info >/dev/null 2>&1; then
  nohup dockerd --storage-driver=overlay2 >/evidence/dockerd.log 2>&1 </dev/null &
fi
for attempt in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then exit 0; fi
  sleep 1
done
exit 1
"""
    worker.exec(["sh", "-c", script], timeout=90).checked("Docker daemon readiness")
