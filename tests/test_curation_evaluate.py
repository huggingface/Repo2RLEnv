from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from repo2rlenv.curation import build_logs, evaluate
from repo2rlenv.curation.budget import Budget
from repo2rlenv.curation.models import CampaignConfig

IMAGE = "im-1pBfFUcERmaI7MH7ZmJbrX"
PRIMARY = f"ImageBuildError: {IMAGE}; use modal image logs {IMAGE}"


class LogProcess:
    def __init__(self, content=b"", *, code=0, blocked=False):
        self.content = content
        self.code = code
        self.blocked = blocked
        self.stdout = self
        self.returncode = None
        self.killed = False
        self.read_bytes = 0

    async def read(self, size):
        if self.blocked:
            await asyncio.Event().wait()
        chunk, self.content = self.content[:size], self.content[size:]
        self.read_bytes += len(chunk)
        return chunk

    async def wait(self):
        self.returncode = self.code if self.returncode is None else self.returncode
        return self.returncode

    async def communicate(self):
        await self.wait()
        return b"", None

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.mark.asyncio
async def test_modal_build_log_uses_fixed_argv_and_redacts_credentials(tmp_path, monkeypatch):
    calls = []
    secret = "private-modal-token-value"
    monkeypatch.setenv("MODAL_TOKEN_SECRET", secret)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unrelated-provider-secret")
    process = LogProcess(
        f"token={secret}\nhttps://user:password@packages.example/simple\nResolutionImpossible: huggingface_hub>=0.34.0,<1.0 conflicts with 0.25.2\n".encode()
    )

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(build_logs.asyncio, "create_subprocess_exec", spawn)
    assert await build_logs.collect_modal_build_log(PRIMARY, tmp_path)
    assert calls[0][0] == (sys.executable, "-m", "modal", "image", "logs", IMAGE, "--layers", "1")
    assert "ANTHROPIC_API_KEY" not in calls[0][1]["env"]
    text = (tmp_path / "build.log").read_text()
    assert secret not in text and "user:password" not in text
    assert "ResolutionImpossible" in text
    assert "Log retrieval completed" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [
        "im-short",
        "im-" + "a" * 23,
        "im-" + "a" * 21 + "_",
        IMAGE + "suffix",
        IMAGE + " " + "im-" + "b" * 22,
    ],
)
async def test_invalid_or_ambiguous_image_id_never_launches_command(
    tmp_path, monkeypatch, identity
):
    async def unexpected(*args, **kwargs):
        pytest.fail("Invalid image identity must not launch a process")

    monkeypatch.setattr(build_logs.asyncio, "create_subprocess_exec", unexpected)
    assert await build_logs.collect_modal_build_log("ImageBuildError: " + identity, tmp_path)
    assert "No unambiguous valid Modal image ID" in (tmp_path / "build.log").read_text()


@pytest.mark.asyncio
async def test_non_build_error_does_not_attempt_log_lookup(tmp_path, monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("An unrelated failure must not fetch image logs")

    monkeypatch.setattr(build_logs.asyncio, "create_subprocess_exec", unexpected)
    assert not await build_logs.collect_modal_build_log("Solver failed: " + IMAGE, tmp_path)
    assert not (tmp_path / "build.log").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "cap", "exit", "spawn"])
async def test_build_log_retrieval_is_bounded_and_failure_is_recorded(
    tmp_path, monkeypatch, failure
):
    process = LogProcess(
        b"x" * 2048, blocked=failure == "timeout", code=2 if failure == "exit" else 0
    )
    monkeypatch.setattr(build_logs, "BUILD_LOG_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(build_logs, "MAX_BUILD_LOG_READ_BYTES", 1024)
    monkeypatch.setattr(build_logs, "MAX_BUILD_LOG_BYTES", 512)
    if failure == "exit":
        process.content = b"Log service unavailable"

    async def spawn(*args, **kwargs):
        if failure == "spawn":
            raise OSError("CLI launch failed")
        return process

    monkeypatch.setattr(build_logs.asyncio, "create_subprocess_exec", spawn)
    assert await build_logs.collect_modal_build_log(PRIMARY, tmp_path)
    text = (tmp_path / "build.log").read_text()
    assert len(text.encode()) <= 512
    expected = {
        "timeout": "timed out",
        "cap": "Read limit reached",
        "exit": "exited with code 2",
        "spawn": "Log retrieval failed",
    }
    assert expected[failure] in text
    if failure in {"timeout", "cap"}:
        assert process.killed
    assert process.read_bytes <= 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("secondary", ["export", "malformed"])
async def test_trial_preserves_primary_build_error_and_delivers_build_tail(
    tmp_path, monkeypatch, secondary
):
    from harbor.trial.trial import Trial

    task = tmp_path / "task"
    task.mkdir()
    (task / "instruction.md").write_text("Task")
    seen = []

    class Runtime:
        async def run(self):
            return SimpleNamespace(
                exception_info=SimpleNamespace(
                    exception_type="ImageBuildError", exception_message=PRIMARY.split(": ", 1)[1]
                ),
                verifier_result=None,
                agent_result=None,
            )

    async def create(config):
        folder = Path(config.trials_dir) / config.trial_name
        (folder / "artifacts").mkdir(parents=True)
        (folder / "artifacts/manifest.json").write_text(
            "{"
            if secondary == "malformed"
            else json.dumps(
                [
                    {"source": "/workspace/src", "status": "failed"},
                ]
            )
        )
        return Runtime()

    async def collect(error, folder):
        seen.append(error)
        (folder / "build.log").write_text(
            "irrelevant earlier output\n" * 1000
            + "Dependency conflict: huggingface_hub>=0.34.0,<1.0 versus pinned 0.25.2\n"
        )
        return True

    monkeypatch.setattr(Trial, "create", create)
    monkeypatch.setattr(evaluate, "collect_modal_build_log", collect)
    result = await evaluate.trial(
        task,
        tmp_path / "trials",
        "baseline",
        config=CampaignConfig(),
        budget=Budget(tmp_path / "budget.json", 10),
        script="true",
    )
    assert result.error.startswith(PRIMARY + "\nSecondary inspection: ")
    assert (
        "Submission export failed" in result.error
        if secondary == "export"
        else "Execution inspection failed" in result.error
    )
    assert seen == [result.error]
    summary = json.loads(evaluate.evidence_summary([result]))[0]
    assert summary["error"].startswith(PRIMARY)
    assert (
        "huggingface_hub>=0.34.0,<1.0 versus pinned 0.25.2" in summary["verifier_logs"]["build.log"]
    )
    assert len(summary["verifier_logs"]["build.log"]) <= 14000


@pytest.mark.asyncio
async def test_thrown_build_error_survives_log_persistence_failure(tmp_path, monkeypatch):
    from harbor.trial.trial import Trial

    class ImageBuildError(Exception):
        pass

    async def create(config):
        raise ImageBuildError(IMAGE)

    async def collect(error, folder):
        raise PermissionError("cannot persist retrieved logs")

    task = tmp_path / "task"
    task.mkdir()
    monkeypatch.setattr(Trial, "create", create)
    monkeypatch.setattr(evaluate, "collect_modal_build_log", collect)
    result = await evaluate.trial(
        task,
        tmp_path / "trials",
        "baseline",
        config=CampaignConfig(),
        budget=Budget(tmp_path / "budget.json", 10),
        script="true",
    )
    assert (
        result.error
        == f"ImageBuildError: {IMAGE}\nBuild log retrieval unavailable (PermissionError)."
    )
