"""Daytona generation worker with explicit Docker readiness probing by the caller."""

from __future__ import annotations

import shlex
from pathlib import Path

from repo2rlenv.execution.base import CommandResult, WorkerSpec


class DaytonaWorker:
    def __init__(self, client, sandbox):
        self.client = client
        self.sandbox = sandbox
        self.id = sandbox.id

    @classmethod
    def create(cls, spec: WorkerSpec) -> DaytonaWorker:
        from daytona import CreateSandboxFromImageParams, Daytona, Image, Resources

        client = Daytona()
        image = spec.image or Image.base("ubuntu:24.04").run_commands(
            "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "docker.io docker-buildx docker-compose-v2 python3 python3-venv git curl ca-certificates",
            "mkdir -p /work /evidence",
            "python3 -m venv /opt/repo2rlenv && /opt/repo2rlenv/bin/python -m pip install uv==0.10.9",
        ).env({"PATH": "/opt/repo2rlenv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"})
        sandbox = client.create(
            CreateSandboxFromImageParams(
                image=image,
                name=spec.name,
                os_user="root",
                resources=Resources(
                    cpu=spec.cpus, memory=(spec.memory_mb + 1023) // 1024, disk=spec.disk_gb
                ),
                labels={"repo2rlenv-worker": spec.name},
                auto_stop_interval=max(1, spec.timeout_sec // 60),
                auto_delete_interval=0,
                ephemeral=True,
            ),
            timeout=180,
        )
        return cls(client, sandbox)

    @classmethod
    def connect(cls, worker_id: str) -> DaytonaWorker:
        from daytona import Daytona

        client = Daytona()
        return cls(client, client.get(worker_id))

    def exec(
        self, argv: list[str], *, timeout: int = 600, env: dict[str, str] | None = None
    ) -> CommandResult:
        if not argv or timeout <= 0:
            raise ValueError("Remote execution needs an argv and positive timeout")
        result = self.sandbox.process.exec(shlex.join(argv), env=env, timeout=timeout)
        return CommandResult(result.exit_code, result.result or "")

    def upload(self, local: Path, remote: str) -> None:
        if not local.is_file() or local.is_symlink():
            raise ValueError("Upload requires a regular local file")
        self.sandbox.fs.upload_file(str(local), remote)

    def download(self, remote: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        self.sandbox.fs.download_file(remote, str(local))

    def terminate(self) -> None:
        self.client.delete(self.sandbox, timeout=180)
