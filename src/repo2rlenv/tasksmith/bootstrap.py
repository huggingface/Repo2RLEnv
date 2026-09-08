from __future__ import annotations

import asyncio
import fcntl
import json
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from repo2rlenv.curation.artifacts import pin_docker_base
from repo2rlenv.curation.budget import Budget
from repo2rlenv.tasksmith.providers import BuildSpec, RemoteWorkspace, WorkspaceConfig
from repo2rlenv.tasksmith.worker import canonical_digest, save_json


def foundation_recipe() -> str:
    return (
        f"FROM {pin_docker_base('python:3.12-slim')}\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends "
        "git curl ca-certificates build-essential ripgrep "
        "&& rm -rf /var/lib/apt/lists/*\n"
    )


def cpu_estimate(provider: str, seconds: float, cpus=2, memory_mib=8192, containers=1) -> float:
    """Conservative elapsed allocation estimate; invoices remain authoritative.

    Rates checked 2026-09-08: modal.com/pricing Sandbox pricing, daytona.io/pricing.
    Include fixed build exposure; distinguish these estimates from metered LLM costs.
    """
    if provider == "modal":
        rate = cpus * 0.00003942 + memory_mib / 1024 * 0.00000667
    elif provider == "daytona":
        rate = (cpus * 0.0504 + memory_mib / 1024 * 0.0162 + 10 * 0.000108) / 3600
        seconds += 90  # SDK0.198 inactivity/cleanup tail; no native wall-clock TTL.
    else:
        raise ValueError("Unknown CPU provider")
    return 0.15 + max(0, seconds) * rate * containers


@asynccontextmanager
async def budgeted_workspace(config: WorkspaceConfig, root: Path, budget: Budget, allowance=2.0):
    """Durably retain create/cleanup receipts and reuse a live identity on recovery."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "resource.json"
    reservation_path = root / "reservation.json"
    prior = json.loads(path.read_text()) if path.exists() else None
    if reservation_path.exists():
        reservation = json.loads(reservation_path.read_text())
        if reservation["config_digest"] != config.digest:
            raise ValueError("Retained workspace configuration changed")
    else:
        reservation = {
            "id": budget.reserve(allowance, f"tasksmith:workspace:{config.operation_id}"),
            "config_digest": config.digest,
            "started_at": time.time(),
            "settled": False,
        }
        save_json(reservation_path, reservation)

    async def receipt(value):
        save_json(path, asdict(value))
        with (root / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(asdict(value)) + "\n")

    workspace = RemoteWorkspace(config, on_receipt=receipt)
    try:
        if prior:
            if prior.get("status") not in {"submitted", "running", "attached"} or not prior.get(
                "resource_id"
            ):
                raise RuntimeError("Retained workspace requires reconciliation; no blind create")
            await workspace.attach(prior["resource_id"])
        else:
            await workspace.start()
        yield workspace
    finally:
        if workspace.resource_id:
            result = await asyncio.shield(workspace.stop())
            if result.status == "terminated" and not reservation["settled"]:
                amount = cpu_estimate(
                    config.provider,
                    time.time() - reservation["started_at"],
                    config.cpus,
                    config.memory_mib,
                )
                budget.settle(reservation["id"], amount, estimated=True)
                reservation.update(settled=True, estimated_usd=amount)
                save_json(reservation_path, reservation)


class BootstrapCache:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def identity(self, recipe: str, inputs: dict) -> str:
        return canonical_digest(
            {"recipe": recipe, "inputs": inputs, "architecture": "linux/amd64", "policy": 1}
        )

    def lookup(self, key: str, provider: str) -> dict | None:
        path = self.root / key / f"{provider}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        if data.get("key") != key or data.get("provider") != provider:
            raise ValueError("Cache identity mismatch")
        return data

    def save(self, key: str, provider: str, recipe: str, inputs: dict, receipt: dict) -> None:
        if receipt.get("status") != "running" or not receipt.get("image_id"):
            raise ValueError("Only a confirmed ready image may enter the bootstrap cache")
        save_json(
            self.root / key / f"{provider}.json",
            {
                "key": key,
                "provider": provider,
                "recipe": recipe,
                "inputs": inputs,
                "image_id": receipt["image_id"],
                "receipt": receipt,
            },
        )

    @asynccontextmanager
    async def claim(self, key: str):
        """One builder per recipe across processes, without blocking the event loop."""
        path = self.root / f"{key}.lock"
        with path.open("a+") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.2)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def dependency_build(recipe: str, cached: dict | None, provider: str) -> BuildSpec:
    if cached:
        return BuildSpec(
            kind="modal-image" if provider == "modal" else "daytona-snapshot",
            role="bootstrap",
            reference=cached["image_id"],
        )
    return BuildSpec(kind="recipe", role="bootstrap", files=(("Dockerfile", recipe.encode()),))
