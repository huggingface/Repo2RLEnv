from __future__ import annotations

import json
import shlex
from pathlib import Path


class AuthorSandbox:
    """Remote-only repository exploration; provider handles all image builds."""

    def __init__(self, timeout: int = 3600):
        self.timeout, self.sandbox = timeout, None

    async def start(self):
        import modal

        app = await modal.App.lookup.aio("repo2rlenv-curation", create_if_missing=True)
        image = modal.Image.debian_slim(python_version="3.12").apt_install(
            "git", "curl", "ca-certificates", "build-essential", "ripgrep"
        )
        self.sandbox = await modal.Sandbox.create.aio(
            "sleep",
            "infinity",
            app=app,
            image=image,
            timeout=self.timeout,
            cpu=2,
            memory=4096,
            workdir="/",
            tags={"purpose": "repo2rlenv-author"},
        )
        return self

    async def shell(self, command: str, timeout_sec: int = 120) -> str:
        p = await self.sandbox.exec.aio(
            "bash", "-lc", command, timeout=max(1, min(timeout_sec, 300))
        )
        import asyncio

        out, err = await asyncio.gather(p.stdout.read.aio(), p.stderr.read.aio())
        code = await p.wait.aio()
        return json.dumps({"exit_code": code, "stdout": out[-20000:], "stderr": err[-4000:]})

    async def write(self, path: str, text: str) -> None:
        await self.sandbox.filesystem.write_text.aio(text, path)

    async def export(self, destination: Path) -> None:
        """Copy only bounded regular files. Never extract agent-authored archives."""
        result = json.loads(
            await self.shell(
                "python - <<'PY'\nfrom pathlib import Path\nimport json\n"
                "p=Path('/output/task')\n"
                "files=list(p.rglob('*'))\n"
                "assert not any(f.is_symlink() for f in files), 'symlink in task'\n"
                "files=[f for f in files if f.is_file()]\n"
                "assert len(files)<150 and sum(f.stat().st_size for f in files)<20000000\n"
                "print(json.dumps([str(f.relative_to(p)) for f in files]))\nPY"
            )
        )
        if result["exit_code"] != 0:
            raise ValueError(f"Cannot export candidate: {result}")
        files = json.loads(result["stdout"])
        destination.mkdir(parents=True, exist_ok=True)
        for relative in files:
            p = Path(relative)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError("Invalid artifact path")
            data = await self.sandbox.filesystem.read_bytes.aio("/output/task/" + relative)
            target = destination / p
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    async def prepare(self, source: dict) -> None:
        repo, base, head = source["repo"], source["base_sha"], source["head_sha"]
        command = (
            "mkdir -p /private /output/task /workspace && "
            f"git clone --filter=blob:none --no-checkout {shlex.quote('https://github.com/' + repo + '.git')} /workspace/repo && "
            f"cd /workspace/repo && git fetch origin {shlex.quote(base)} {shlex.quote(head)} && "
            f"git checkout --detach {shlex.quote(base)} && "
            f"git diff {shlex.quote(base)} {shlex.quote(head)} > /private/gold.patch"
        )
        result = json.loads(await self.shell(command, 300))
        if result["exit_code"]:
            raise RuntimeError(f"Repository preparation failed: {result}")
        await self.write("/private/pr.json", json.dumps(source, indent=2))

    async def stop(self) -> None:
        if self.sandbox:
            await self.sandbox.terminate.aio()
