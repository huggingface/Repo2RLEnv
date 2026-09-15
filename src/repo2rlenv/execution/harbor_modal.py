"""Budgeted, permanently offline native Modal environments for Harbor GPU trials.

Harbor supplies image construction, command execution and artifact transfer. This
adapter adds durable allocation receipts, a single dispatch and confirmed cleanup.
It is used by the trusted controller; target code only runs on Modal.
"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from importlib.metadata import version
from pathlib import Path

from harbor.environments.modal import ModalEnvironment
from harbor.models.task.config import NetworkMode

from repo2rlenv.execution.lifecycle import now, save_record
from repo2rlenv.quality.loop.client import RunBudget
from repo2rlenv.tasksmith.build_logs import build_excerpt, redact_build_text, save_build_logs

_IMAGE_LOG_TIMEOUT_SEC = 10
_IMAGE_LOG_MAX_BYTES = 4 * 1024 * 1024
_IMAGE_LOG_MAX_ENTRIES = 10_000


@dataclass
class NativeAccounting:
    budget: RunBudget
    directory: Path
    reservation_usd: str = "3.00"
    environments: list = field(default_factory=list)


ACCOUNTING: ContextVar[NativeAccounting | None] = ContextVar(
    "native_modal_accounting", default=None
)


def compute_estimate(record: dict) -> dict:
    """Charge conservative allocation time, including build wait, after cleanup."""
    if record["state"] not in {"terminated", "build_failed"}:
        raise ValueError("Native compute requires confirmed cleanup or a failed image build")
    seconds = Decimal(
        str(
            (
                datetime.fromisoformat(record["stopped_at"])
                - datetime.fromisoformat(record["started_at"])
            ).total_seconds()
        )
    )
    spec = record["spec"]
    rate = (
        Decimal("0.00003942") * spec["cpus"]
        + Decimal("0.00000667") * spec["memory_mb"] / 1024
        + Decimal("0.000222") * spec["gpus"]
    )
    return {
        "kind": "conservative_estimate_not_invoice",
        "pricing_source": "https://modal.com/pricing",
        "pricing_checked_on": "2026-09-13",
        "seconds_including_build_wait": str(seconds),
        "pricing_product": "Modal Sandboxes and Notebooks",
        "allocation_rate_per_second": str(rate),
        "safety_factor": 2,
        "build_allowance_usd": "0.25",
        "accounted_usd": str(
            (seconds * rate * 2 + Decimal("0.25")).quantize(
                Decimal("0.000001"), rounding=ROUND_CEILING
            )
        ),
    }


class MeteredModalEnvironment(ModalEnvironment):
    async def _sdk_upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        from repo2rlenv.execution.modal_transfer import upload_directory

        await upload_directory(self, source_dir, target_dir)

    def __init__(self, *args, **kwargs):
        if version("harbor") != "0.22.0":
            raise RuntimeError("Native GPU execution requires the tested Harbor 0.22.0 contract")
        self.accounting = ACCOUNTING.get()
        if self.accounting is None:
            raise ValueError("Native GPU execution requires a controller budget context")
        super().__init__(*args, **kwargs)
        if (
            self._compose_mode
            or self._vm_runtime_enabled
            or self._sandbox_v2_enabled
            or self._dynamic_network
        ):
            raise ValueError(
                "GPU trials require a native single-container environment with a fixed network policy"
            )
        if (
            self.network_policy.network_mode != NetworkMode.NO_NETWORK
            or self.network_policy.allowed_hosts
        ):
            raise ValueError("GPU trials must be offline")
        if self._secrets or self._volumes or self._registry_secret:
            raise ValueError(
                "GPU task sandboxes cannot receive controller secrets or shared volumes"
            )
        if self._gpu_config() not in {"L4:1", "L4:2"}:
            raise ValueError("This GPU execution profile requires one or two L4 devices")
        if not 60 <= self._sandbox_timeout <= 1800:
            raise ValueError("GPU sandboxes need a bounded timeout of at most 1800 seconds")
        self.record = None
        self.receipt = self.accounting.directory / (self.session_id + ".json")
        self.accounting.environments.append(self)

    async def _create_sandbox(
        self, *, entrypoint=None, block_network=None, experimental_options=None
    ):
        import modal

        if block_network is False or experimental_options:
            raise ValueError("GPU execution cannot relax the offline native sandbox contract")
        if self.receipt.exists():
            raise ValueError("Allocation already claimed; reconcile its stable provider name")
        operation = "native:" + self.accounting.budget.prefix + ":" + self.session_id
        record = {
            "state": "claimed",
            "operation_id": operation,
            "started_at": now(),
            "provider": "modal",
            "provider_name": self.session_id,
            "app_name": self._app_name,
            "ledger": str(self.accounting.budget.path.resolve()),
            "spec": {
                "cpus": self.task_env_config.cpus,
                "memory_mb": self.task_env_config.memory_mb,
                "gpus": self._effective_gpus,
                "gpu_type": "L4",
                "network": "no-network",
            },
        }
        save_record(self.receipt, record)
        try:
            self.accounting.budget.reserve(
                operation, self.accounting.reservation_usd, "Native Harbor GPU image and sandbox"
            )
        except Exception:
            record["state"] = "reservation_failed"
            save_record(self.receipt, record)
            raise
        self.record = record
        record["state"] = "creating"
        save_record(self.receipt, record)
        try:
            # No blind SDK/application retry after an uncertain create. The name
            # in the receipt is the provider recovery handle.
            sandbox = await modal.Sandbox.create.aio(
                *(entrypoint or ("sleep", "infinity")),
                app=self._app,
                image=self._image,
                name=self.session_id,
                timeout=self._sandbox_timeout,
                block_network=True,
                cpu=(self.task_env_config.cpus, self.task_env_config.cpus),
                memory=self.task_env_config.memory_mb,
                gpu=self._gpu_config(),
                tags=self._sandbox_labels(),
            )
            self._sandbox = sandbox
            record.update(
                state="running",
                worker_id=sandbox.object_id,
                image_id=self._image.object_id,
                ready_at=now(),
            )
            save_record(self.receipt, record)
            return sandbox
        except modal.exception.ImageBuildError as error:
            record.update(
                state="build_failed",
                stopped_at=now(),
                no_sandbox_dispatched=True,
                image_id=error.image_id,
            )
            self._settle()
            # Diagnostics are read-only and follow confirmed failure settlement.
            # A logging failure must never turn this into an uncertain allocation.
            message = redact_build_text(str(error))
            try:
                diagnostics = await self._image_build_diagnostics(error.image_id)
            except Exception as diagnostic_error:
                diagnostics = (
                    "Image build diagnostics unavailable ("
                    + type(diagnostic_error).__name__
                    + "); the failed build remains settled."
                )
            error.args = (build_excerpt(message + "\n" + diagnostics),)
            raise
        except BaseException:
            record["state"] = "creation_uncertain"
            save_record(self.receipt, record)
            self.accounting.budget.mark_uncertain(
                operation, f"Inspect native allocation by provider name; {self.receipt}"
            )
            raise

    async def _image_build_diagnostics(self, image_id: str) -> str:
        import modal

        streams: dict[str, list[str]] = {"stdout": [], "stderr": []}
        fetch = {
            "image_id": image_id,
            "layers": None,
            "status": "complete",
            "entries": 0,
            "bytes_collected": 0,
            "timeout_sec": _IMAGE_LOG_TIMEOUT_SEC,
            "max_bytes": _IMAGE_LOG_MAX_BYTES,
            "max_entries": _IMAGE_LOG_MAX_ENTRIES,
        }
        try:
            async with asyncio.timeout(_IMAGE_LOG_TIMEOUT_SEC):
                # Harbor's App.lookup uses this cached environment client too.
                # from_id plus logs.fetch does not hydrate or rebuild the image.
                client = await modal.Client.from_env.aio()
                image = modal.Image.from_id(image_id, client=client)
                async with aclosing(image.logs.fetch.aio(layers=None)) as entries:
                    async for entry in entries:
                        raw = entry.message.encode()
                        remaining = _IMAGE_LOG_MAX_BYTES - fetch["bytes_collected"]
                        stream = "stdout" if entry.source == "stdout" else "stderr"
                        streams[stream].append(raw[:remaining].decode(errors="ignore"))
                        fetch["entries"] += 1
                        fetch["bytes_collected"] += min(len(raw), remaining)
                        if len(raw) > remaining or fetch["entries"] >= _IMAGE_LOG_MAX_ENTRIES:
                            fetch["status"] = "truncated"
                            break
        except Exception as error:
            fetch["status"] = "timeout" if isinstance(error, TimeoutError) else "failed"
            fetch["error_type"] = type(error).__name__

        text = {name: "".join(chunks) for name, chunks in streams.items()}
        if fetch["status"] != "complete":
            # A truncated credential can span entries. Discard unfinished lines
            # before the shared redactor sees the joined streams.
            text = {name: value[: value.rfind("\n") + 1] for name, value in text.items()}
        directory = self.receipt.with_suffix(".image-build")
        directory.mkdir(parents=True, exist_ok=True)
        prefix = directory / "build"
        excerpt = save_build_logs(prefix, text["stdout"], text["stderr"])
        log_receipt = prefix.with_suffix(".logs.json")
        fetch["logs_receipt"] = str(log_receipt.resolve())
        fetch["logs_receipt_sha256"] = hashlib.sha256(log_receipt.read_bytes()).hexdigest()
        fetch_receipt = directory / "fetch.json"
        save_record(fetch_receipt, fetch)
        self.record["build_logs"] = {
            "path": str(fetch_receipt.resolve()),
            "sha256": hashlib.sha256(fetch_receipt.read_bytes()).hexdigest(),
        }
        save_record(self.receipt, self.record)
        return f"Modal image build logs ({fetch['status']}; receipt {fetch_receipt}):\n" + (
            excerpt or "No complete log lines were available."
        )

    def _settle(self):
        cost = compute_estimate(self.record)
        cost_path = self.receipt.with_suffix(".cost.json")
        save_record(self.receipt, self.record)
        save_record(cost_path, cost)
        self.accounting.budget.settle(
            self.record["operation_id"], cost["accounted_usd"], evidence=str(cost_path.resolve())
        )

    async def stop(self, delete=True):
        if self._sandbox is None or not self.record or self.record["state"] == "terminated":
            return
        self.record["state"] = "terminating"
        save_record(self.receipt, self.record)
        try:
            await self._sandbox.terminate.aio(wait=True)
        except BaseException:
            self.record["state"] = "termination_uncertain"
            save_record(self.receipt, self.record)
            self.accounting.budget.mark_uncertain(
                self.record["operation_id"], f"Native cleanup not confirmed; {self.receipt}"
            )
            raise
        self.record.update(state="terminated", stopped_at=now())
        self._settle()
        self._sandbox = None
