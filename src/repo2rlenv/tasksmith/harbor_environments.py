"""Lifecycle receipts around Harbor adapters, including its separate grader."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from pathlib import Path

from repo2rlenv.tasksmith.providers import BuildSpec
from repo2rlenv.tasksmith.worker import save_json


class TrackedEnvironment:
    def __init__(self, *args, receipt_root: str, absolute_deadline: float, **kwargs):
        self._receipt_root = Path(receipt_root)
        self._absolute_deadline = absolute_deadline
        self._captured_resource = None
        self._resource_id = None
        self._cleanup_confirmed = False
        self._created_at = None
        self._claimed_at = None
        self._receipt_lock = asyncio.Lock()
        super().__init__(*args, **kwargs)
        if not math.isfinite(absolute_deadline):
            raise ValueError("A finite absolute deadline is required")
        if self._receipt_root.is_symlink() or not self._receipt_root.is_absolute():
            raise ValueError("Receipts require an absolute, unlinked host directory")
        self._role = "grader" if "__verifier__" in self.session_id else "solver"
        self._build_digest = BuildSpec.from_directory(
            Path(self.environment_dir), role=self._role
        ).digest
        if getattr(self, "_compose_mode", False) or getattr(self, "_sandbox_v2_enabled", False):
            raise ValueError("Tracked trials support CPU direct environments only")
        if self.task_env_config.gpus or self.task_env_config.tpu:
            raise ValueError("GPU/TPU profiles require separate conformance")

    def _remaining(self, ceiling=3600):
        remaining = min(ceiling, int(self._absolute_deadline - time.time()))
        if remaining <= 0:
            raise TimeoutError("Original task trial deadline expired")
        return remaining

    def _receipt(self, status, error=None):
        key = hashlib.sha256(self.session_id.encode()).hexdigest()[:24]
        value = {
            "provider": self.type().value,
            "session_id": self.session_id,
            "role": self._role,
            "build_digest": self._build_digest,
            "environment_dir": str(self.environment_dir),
            "resource_id": self._resource_id,
            "status": status,
            "observed_at": time.time(),
            "created_at": self._created_at,
            "deadline": self._absolute_deadline,
            "error": error,
            "claimed_at": self._claimed_at,
            "profile": "cpu-direct",
            "cleanup_confirmed": self._cleanup_confirmed,
        }
        save_json(self._receipt_root / f"{key}.json", value)
        with (self._receipt_root / f"{key}.jsonl").open("a") as stream:
            stream.write(json.dumps(value) + "\n")

    async def start(self, force_build=False):
        self._remaining()
        key = hashlib.sha256(self.session_id.encode()).hexdigest()[:24]
        if (self._receipt_root / f"{key}.json").exists():
            raise RuntimeError(
                "Retained provider operation requires reconciliation; no blind restart"
            )
        self._claimed_at = time.time()
        self._receipt("claimed")
        try:
            async with asyncio.timeout(self._remaining()):
                await super().start(force_build)
            self._receipt("running")
        except BaseException as exc:
            self._capture(getattr(self, "_sandbox", None))
            self._receipt("uncertain", type(exc).__name__)
            raise

    def _capture(self, resource):
        if resource is not None:
            resource_id = getattr(resource, "object_id", None) or getattr(resource, "id", None)
            if self._resource_id == resource_id and self._captured_resource is not None:
                return
            self._captured_resource = resource
            self._resource_id = resource_id
            self._created_at = self._created_at or time.time()
            self._receipt("submitted")

    async def exec(self, *args, timeout_sec=None, **kwargs):
        timeout = self._remaining(timeout_sec or 300)
        async with asyncio.timeout(timeout):
            return await super().exec(*args, timeout_sec=timeout, **kwargs)

    async def stop(self, delete=True):
        async with self._receipt_lock:
            await self._stop_once(delete)

    async def _stop_once(self, delete):
        if self._cleanup_confirmed:
            return
        self._capture(getattr(self, "_sandbox", None))
        try:
            async with asyncio.timeout(60):
                await super().stop(delete)
                if self._captured_resource is not None:
                    await self._confirm_terminal()
                elif self._resource_id is not None:
                    raise RuntimeError("Lost resource handle before confirmation")
                else:
                    self._receipt("uncertain", "Creation returned no resource identity")
                    return
            self._cleanup_confirmed = True
            self._receipt("stopped")
        except BaseException as exc:
            self._cleanup_confirmed = False
            self._receipt("uncertain", type(exc).__name__)
            raise


def __getattr__(name):
    """Load only the selected provider SDK; Harbor resolves these module symbols."""
    if name == "TrackedModalEnvironment":
        from harbor.environments.modal import ModalEnvironment

        class TrackedModalEnvironment(TrackedEnvironment, ModalEnvironment):
            def __init__(self, *args, absolute_deadline, **kwargs):
                kwargs["sandbox_timeout_secs"] = max(1, int(absolute_deadline - time.time()))
                super().__init__(*args, absolute_deadline=absolute_deadline, **kwargs)

            async def _create_sandbox(self, **kwargs):
                self._sandbox_timeout = self._remaining()
                # Harbor's retry decorator can duplicate a lost-response create.
                creation = asyncio.create_task(
                    ModalEnvironment._create_sandbox.__wrapped__(self, **kwargs)
                )
                try:
                    resource = await asyncio.shield(creation)
                except asyncio.CancelledError:
                    try:
                        resource = await asyncio.wait_for(creation, timeout=30)
                        self._capture(resource)
                        self._sandbox = resource
                    except BaseException:
                        self._receipt(
                            "uncertain", "Cancelled create without a confirmed resource identity"
                        )
                    raise
                self._capture(resource)
                return resource

            async def _confirm_terminal(self):
                from modal import Sandbox
                from modal.exception import NotFoundError

                try:
                    resource = await Sandbox.from_id.aio(self._resource_id)
                except NotFoundError:
                    return
                if await resource.poll.aio() is None:
                    await resource.terminate.aio()
                    await resource.wait.aio(raise_on_termination=False)
                if await resource.poll.aio() is None:
                    raise RuntimeError("Modal resource still running after cleanup")

        return TrackedModalEnvironment
    if name == "TrackedDaytonaEnvironment":
        from daytona.common.errors import DaytonaNotFoundError
        from harbor.environments.daytona import DaytonaEnvironment

        class TrackedDaytonaEnvironment(TrackedEnvironment, DaytonaEnvironment):
            def __init__(self, *args, **kwargs):
                kwargs["auto_stop_interval"] = 1
                kwargs["auto_delete_interval"] = 0
                super().__init__(*args, **kwargs)

            async def _create_sandbox(self, *args, **kwargs):
                try:
                    return await DaytonaEnvironment._create_sandbox.__wrapped__(
                        self, *args, **kwargs
                    )
                finally:
                    self._capture(getattr(self, "_sandbox", None))

            async def _confirm_terminal(self):
                client = await self._client_manager.get_client()
                try:
                    sandbox = await client.get(self._resource_id)
                except DaytonaNotFoundError:
                    return
                await client.delete(sandbox, timeout=30)
                for _ in range(100):
                    try:
                        sandbox = await client.get(self._resource_id)
                    except DaytonaNotFoundError:
                        return
                    if getattr(sandbox.state, "value", sandbox.state) == "destroyed":
                        return
                    await asyncio.sleep(0.25)
                raise RuntimeError("Daytona deletion not confirmed")

        return TrackedDaytonaEnvironment
    raise AttributeError(name)
