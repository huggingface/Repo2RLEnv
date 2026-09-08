from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("modal")
pytest.importorskip("daytona")

from repo2rlenv.tasksmith import providers as p


def config(provider="modal", **kw):
    return p.WorkspaceConfig(
        provider=provider,
        operation_id="fixture-operation",
        deadline=time.time() + 300,
        build=p.BuildSpec(kind="oci", role="author", reference="python@sha256:" + "a" * 64),
        **kw,
    )


def fake_modal():
    running = {"value": True}
    process = SimpleNamespace(
        stdout=SimpleNamespace(
            read=SimpleNamespace(aio=AsyncMock(return_value='{"exit_code":0,"stdout":"ok"}'))
        ),
        stderr=SimpleNamespace(read=SimpleNamespace(aio=AsyncMock(return_value=""))),
        wait=SimpleNamespace(aio=AsyncMock(return_value=0)),
    )

    async def terminate():
        running["value"] = False

    handle = SimpleNamespace(
        object_id="sb-fixture",
        exec=SimpleNamespace(aio=AsyncMock(return_value=process)),
        get_tags=SimpleNamespace(aio=AsyncMock()),
        poll=SimpleNamespace(aio=AsyncMock(side_effect=lambda: None if running["value"] else 0)),
        terminate=SimpleNamespace(aio=AsyncMock(side_effect=terminate)),
    )
    image = SimpleNamespace(object_id="im-fixture", build=SimpleNamespace(aio=AsyncMock()))
    image.build.aio.return_value = image
    sdk = SimpleNamespace(
        App=SimpleNamespace(lookup=SimpleNamespace(aio=AsyncMock(return_value="app"))),
        Image=SimpleNamespace(
            from_registry=lambda ref: image,
            from_id=lambda ref: image,
            from_dockerfile=lambda *a, **k: image,
        ),
        Sandbox=SimpleNamespace(
            create=SimpleNamespace(aio=AsyncMock(return_value=handle)),
            from_id=SimpleNamespace(aio=AsyncMock(return_value=handle)),
        ),
    )
    return p.ModalBackend(sdk), sdk, handle, process


def fake_daytona():
    import daytona

    handle = SimpleNamespace(
        id="daytona-fixture",
        snapshot="snapshot-fixture",
        state="started",
        labels={},
        process=SimpleNamespace(
            exec=AsyncMock(return_value=SimpleNamespace(exit_code=0, result="{}"))
        ),
    )

    async def create(*, params, timeout):
        handle.labels = params.labels
        return handle

    async def delete(sandbox):
        handle.state = "destroyed"

    client = SimpleNamespace(
        create=AsyncMock(side_effect=create),
        delete=AsyncMock(side_effect=delete),
        get=AsyncMock(return_value=handle),
        snapshot=SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(cpu=2, mem=4, disk=10, gpu=0))
        ),
    )
    return p.DaytonaBackend(daytona, client), client, handle


@pytest.mark.asyncio
async def test_modal_records_build_and_resource_before_use_and_confirms_stop():
    backend, sdk, handle, _ = fake_modal()
    receipts = []

    async def record(receipt):
        if receipt.status == "claimed":
            sdk.Sandbox.create.aio.assert_not_awaited()
        receipts.append(receipt)

    cfg = config(network="none")
    workspace = p.RemoteWorkspace(cfg, record, backend=backend)
    await workspace.start()
    assert [r.status for r in receipts] == ["claimed", "image-ready", "submitted", "running"]
    assert receipts[2].resource_id == "sb-fixture"
    assert receipts[2].image_id == "im-fixture"
    args = sdk.Sandbox.create.aio.await_args.kwargs
    assert args["tags"] == cfg.labels and args["block_network"] is True
    assert args["cpu"] == 2 and args["memory"] == 4096
    assert 1 <= args["timeout"] <= 300
    try:
        result = json.loads(await workspace.shell("printf hello", 20))
        assert result["stdout"] == "ok"
        assert handle.exec.aio.await_args.kwargs["timeout"] <= 20
        with pytest.raises(p.ProviderError, match="already attempted"):
            await workspace.start()
    finally:
        stopped = await workspace.stop()
    assert stopped.status == "terminated"
    sdk.Sandbox.from_id.aio.assert_awaited_with("sb-fixture")
    assert await workspace.stop() == stopped


@pytest.mark.asyncio
async def test_daytona_uses_real_pinned_models_and_reports_weaker_deadline():
    backend, client, handle = fake_daytona()
    record = AsyncMock()
    cfg = config("daytona", network="none")
    workspace = await p.RemoteWorkspace(cfg, record, backend=backend).start()
    params = client.create.await_args.kwargs["params"]
    assert params.resources.cpu == 2 and params.resources.memory == 4
    assert params.resources.disk == 10 and params.resources.gpu == 0
    assert params.auto_stop_interval == 1 and params.auto_delete_interval == 0
    assert params.ephemeral and params.network_block_all and not params.public
    assert "ttl_minutes" not in params.model_dump()
    assert workspace.receipt.deadline_enforcement == "controller-watchdog+provider-inactivity"
    assert workspace.receipt.exposure_tail_sec == 90
    assert workspace.receipt.requested_disk_gib == 10
    assert (await workspace.stop()).status == "terminated"
    client.get.assert_awaited_with(handle.id)


@pytest.mark.asyncio
async def test_unsupported_daytona_ttl_rejected_before_claim_or_api_call():
    backend, client, _ = fake_daytona()
    record = AsyncMock()
    with pytest.raises(ValueError, match="wall-clock TTL"):
        await p.RemoteWorkspace(
            config("daytona", require_native_deadline=True), record, backend=backend
        ).start()
    record.assert_not_awaited()
    client.create.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        {"provider": "docker"},
        {"profile": "gpu"},
        {"cpus": 0},
        {"memory_mib": 10},
        {"network": "allowlist"},
        {"max_export_bytes": 100_000_000},
    ],
)
def test_unsupported_configuration_is_rejected(change):
    with pytest.raises(ValueError):
        replace(config(), **change)


@pytest.mark.asyncio
async def test_expired_deadline_and_failed_claim_have_no_effects():
    backend, sdk, _, _ = fake_modal()
    record = AsyncMock()
    cfg = replace(config(), deadline=time.time() - 1)
    with pytest.raises(p.DeadlineExceeded):
        await p.RemoteWorkspace(cfg, record, backend=backend).start()
    record.assert_not_awaited()
    sdk.App.lookup.aio.assert_not_awaited()
    record.side_effect = OSError("journal unavailable")
    with pytest.raises(OSError, match="journal unavailable"):
        await p.RemoteWorkspace(config(), record, backend=backend).start()
    sdk.App.lookup.aio.assert_not_awaited()


@pytest.mark.asyncio
async def test_attach_checks_labels_without_restart_or_deadline_extension():
    backend, sdk, handle, _ = fake_modal()
    cfg = config()
    handle.get_tags.aio.return_value = cfg.labels
    workspace = await p.RemoteWorkspace(cfg, AsyncMock(), backend=backend).attach("sb-fixture")
    assert workspace.receipt.deadline == cfg.deadline
    sdk.Sandbox.create.aio.assert_not_awaited()
    await workspace.stop()
    backend, sdk, handle, _ = fake_modal()
    handle.get_tags.aio.return_value = {"tasksmith.config": "wrong"}
    workspace = p.RemoteWorkspace(cfg, AsyncMock(), backend=backend)
    with pytest.raises(p.ProviderError, match="mismatch"):
        await workspace.attach("someone-elses-sandbox")
    assert workspace.resource_id is None
    handle.terminate.aio.assert_not_awaited()


@pytest.mark.asyncio
async def test_stopped_attach_never_starts_it_again():
    backend, client, handle = fake_daytona()
    cfg = config("daytona")
    handle.labels = cfg.labels
    handle.state = "stopped"
    workspace = p.RemoteWorkspace(cfg, AsyncMock(), backend=backend)
    with pytest.raises(p.ProviderError, match="no implicit restart"):
        await workspace.attach(handle.id)
    client.create.assert_not_awaited()
    await workspace.stop()


@pytest.mark.asyncio
async def test_cleanup_only_attachment_reconciles_after_original_deadline():
    backend, sdk, handle, _ = fake_modal()
    cfg = replace(config(), deadline=time.time() - 100)
    handle.get_tags.aio.return_value = cfg.labels
    workspace = await p.RemoteWorkspace(cfg, AsyncMock(), backend=backend).attach(
        "sb-fixture", cleanup_only=True
    )
    assert not workspace._ready
    assert (await workspace.stop()).status == "terminated"
    sdk.Sandbox.create.aio.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_create_after_caller_cancellation_is_receipted_and_cleaned():
    backend, sdk, handle, _ = fake_modal()
    entered, release = asyncio.Event(), asyncio.Event()

    async def create(*args, **kwargs):
        entered.set()
        await release.wait()
        return handle

    sdk.Sandbox.create.aio.side_effect = create
    record = AsyncMock()
    workspace = p.RemoteWorkspace(config(), record, backend=backend)
    start = asyncio.create_task(workspace.start())
    await entered.wait()
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert workspace.receipt.status == "uncertain"
    release.set()
    await workspace._creation_task
    assert workspace.receipt.status == "terminated"
    assert any(
        c.args[0].status == "submitted" and c.args[0].resource_id == "sb-fixture"
        for c in record.await_args_list
    )
    handle.terminate.aio.assert_awaited_once()


@pytest.mark.asyncio
async def test_unconfirmed_cleanup_retains_identity_and_never_reports_success():
    backend, _, handle, _ = fake_modal()
    workspace = await p.RemoteWorkspace(
        config(cleanup_timeout_sec=1), AsyncMock(), backend=backend
    ).start()
    handle.terminate.aio.side_effect = OSError("lost response")
    backend.terminal = AsyncMock(side_effect=OSError("lookup unavailable"))
    with pytest.raises(p.CleanupUncertain):
        await workspace.stop()
    assert workspace.resource_id == "sb-fixture" and workspace.receipt.status == "uncertain"
    backend.terminal = AsyncMock(return_value=True)
    assert (await workspace.stop()).status == "terminated"


@pytest.mark.asyncio
async def test_daytona_stopped_state_is_not_deleted_and_notfound_is_confirmed():
    from daytona.common.errors import DaytonaNotFoundError

    backend, client, handle = fake_daytona()
    handle.state = "stopped"
    assert not await backend.terminal(handle.id)
    client.get.side_effect = DaytonaNotFoundError("deleted")
    assert await backend.terminal(handle.id)


@pytest.mark.asyncio
async def test_command_deadline_enforced_but_cleanup_remains_available(monkeypatch):
    backend, _, handle, _ = fake_modal()
    cfg = config()
    workspace = await p.RemoteWorkspace(cfg, AsyncMock(), backend=backend).start()
    monkeypatch.setattr(p.time, "time", lambda: cfg.deadline + 1)
    with pytest.raises(p.DeadlineExceeded):
        await workspace.shell("echo should-not-run")
    handle.exec.aio.assert_not_awaited()
    assert (await workspace.stop()).status == "terminated"


@pytest.mark.asyncio
async def test_deadline_watchdog_stops_without_another_caller_operation(monkeypatch):
    backend, _, handle, _ = fake_modal()
    cfg = config()
    workspace = await p.RemoteWorkspace(cfg, AsyncMock(), backend=backend).start()
    monkeypatch.setattr(p.time, "time", lambda: cfg.deadline + 1)
    await workspace._deadline_stop()
    handle.terminate.aio.assert_awaited_once()
    assert workspace.receipt.status == "terminated"


@pytest.mark.asyncio
async def test_failed_terminal_journal_is_not_silently_treated_as_durable_success():
    backend, _, handle, _ = fake_modal()
    record = AsyncMock()
    workspace = await p.RemoteWorkspace(config(), record, backend=backend).start()
    record.side_effect = OSError("journal unavailable")
    with pytest.raises(p.CleanupUncertain):
        await workspace.stop()
    handle.terminate.aio.assert_awaited_once()
    assert workspace._persisted_status != "terminated"
    record.side_effect = None
    assert (await workspace.stop()).status == "terminated"
    assert workspace._persisted_status == "terminated"


@pytest.mark.asyncio
async def test_callback_failure_after_create_still_cleans_up_known_resource():
    backend, _, handle, _ = fake_modal()

    async def record(receipt):
        if receipt.status == "submitted":
            raise OSError("resource receipt write failed")

    workspace = p.RemoteWorkspace(config(), record, backend=backend)
    with pytest.raises(OSError):
        await workspace.start()
    await workspace._emergency_cleanup
    handle.terminate.aio.assert_awaited_once()
    assert workspace._persisted_status == "terminated"


def test_build_context_captures_bytes_and_rejects_symlinks_and_cross_provider_ids(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python@sha256:" + "a" * 64)
    (tmp_path / "data").write_bytes(b"abc")
    (tmp_path / "data").chmod(0o755)
    build = p.BuildSpec.from_directory(tmp_path, role="author")
    original = build.digest
    assert build.executables == ("data",)
    assert replace(build, executables=()).digest != original
    (tmp_path / "data").write_bytes(b"changed")
    assert build.digest == original
    (tmp_path / "link").symlink_to(tmp_path / "data")
    with pytest.raises(ValueError, match="Nonregular"):
        p.BuildSpec.from_directory(tmp_path, role="author")
    with pytest.raises(ValueError, match="cannot"):
        replace(
            config("daytona"),
            build=p.BuildSpec(kind="modal-image", role="author", reference="im-x"),
        )
    with pytest.raises(ValueError, match="immutable"):
        p.BuildSpec(kind="oci", role="author", reference="python:latest")


@pytest.mark.asyncio
async def test_daytona_recipe_context_and_snapshot_resource_preflight(tmp_path):
    backend, client, _ = fake_daytona()
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\nCOPY data /data\n")
    (tmp_path / "data").write_bytes(b"data")
    cfg = replace(config("daytona"), build=p.BuildSpec.from_directory(tmp_path, role="author"))
    await backend.create(cfg, tmp_path, lambda: 30, AsyncMock())
    params = client.create.await_args.kwargs["params"]
    assert "COPY data /data" in params.image.dockerfile()
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\nCOPY ../outside /outside\n")
    client.create.reset_mock()
    with pytest.raises(ValueError, match="escapes"):
        await backend.create(cfg, tmp_path, lambda: 30, AsyncMock())
    client.create.assert_not_awaited()
    cfg = replace(
        config("daytona"),
        build=p.BuildSpec(kind="daytona-snapshot", role="author", reference="saved"),
    )
    client.snapshot.get.return_value.gpu = 1
    with pytest.raises(ValueError, match="resources"):
        await backend.create(cfg, tmp_path, lambda: 30, AsyncMock())
    client.create.assert_not_awaited()


def invoke_file_helper(argument):
    """Exercise our trusted stdlib transport implementation, never task code."""
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", p._FILE_SCRIPT, json.dumps(argument)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def test_remote_file_helper_rejects_symlink_fifo_and_oversized_files(tmp_path):
    data = tmp_path / "data"
    data.write_bytes(b"hello")
    symlink = tmp_path / "link"
    symlink.symlink_to(data)
    base = {"op": "read", "limit": 100}
    assert invoke_file_helper({**base, "path": str(symlink)}).returncode != 0
    assert invoke_file_helper({**base, "path": str(data), "limit": 3}).returncode != 0
    import os

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert invoke_file_helper({**base, "path": str(fifo)}).returncode != 0
    (tmp_path / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    assert invoke_file_helper({**base, "path": str(tmp_path / "dirlink" / "data")}).returncode != 0
    result = invoke_file_helper({**base, "path": str(data)})
    assert result.returncode == 0, result.stderr
    assert base64.b64decode(json.loads(result.stdout)["data"]) == b"hello"


def test_remote_write_helper_checks_before_truncating_link_and_supports_chunks(tmp_path):
    path = tmp_path / "data"
    path.write_bytes(b"original")
    link = tmp_path / "link"
    link.symlink_to(path)
    arg = {"op": "write", "path": str(link), "offset": 0, "data": base64.b64encode(b"abc").decode()}
    assert invoke_file_helper(arg).returncode != 0
    assert path.read_bytes() == b"original"
    arg["path"] = str(path)
    assert invoke_file_helper(arg).returncode == 0
    arg["offset"] = 3
    assert invoke_file_helper(arg).returncode == 0
    assert path.read_bytes() == b"abcabc"


def test_trusted_shell_transport_retains_exit_status_and_bounds_binary_output():
    # This executes only the transport's own controlled fixture command locally.
    command = "python3 -c " + __import__("shlex").quote(
        "import os,sys; os.write(1,b'x'*25000); os.write(2,b'e'*5000); sys.exit(7)"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            p._SHELL_SCRIPT,
            json.dumps({"command": command, "timeout": 3}),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    output = json.loads(result.stdout)
    assert output["exit_code"] == 7 and not output["timed_out"]
    assert len(output["stdout"]) == 20000 and len(output["stderr"]) == 4000


def row(path="tests/test_contract.py", data=b"assert True"):
    return {
        "path": path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "data": base64.b64encode(data).decode(),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entries",
    [
        [row("../escape")],
        [row("/absolute")],
        [row(), row()],
        [row("tests"), row("tests/test.py")],
        [{**row(), "sha256": "wrong"}],
    ],
)
async def test_untrusted_export_manifest_cannot_write_host_files(tmp_path, entries):
    workspace = p.RemoteWorkspace(config(), AsyncMock())
    workspace._run = AsyncMock(return_value=entries)
    destination = tmp_path / "export"
    with pytest.raises(ValueError):
        await workspace.export(destination)
    assert not destination.exists()


@pytest.mark.asyncio
async def test_export_regular_binary_files_atomically_with_hashes_and_no_symlink_destination(
    tmp_path,
):
    source = tmp_path / "remote"
    source.mkdir()
    (source / "fixture.bin").write_bytes(b"\0\xffbinary")
    (source / "fixture.bin").chmod(0o755)
    result = invoke_file_helper(
        {"op": "export", "path": str(source), "count": 10, "file_limit": 100, "total_limit": 100}
    )
    assert result.returncode == 0, result.stderr
    workspace = p.RemoteWorkspace(config(), AsyncMock())
    workspace._run = AsyncMock(return_value=json.loads(result.stdout))
    target = tmp_path / "export"
    receipt = await workspace.export(target)
    assert (target / "fixture.bin").read_bytes() == b"\0\xffbinary"
    assert receipt[0]["size"] == 8
    assert receipt[0]["executable"]
    assert (target / "fixture.bin").stat().st_mode & 0o111
    linked = tmp_path / "linked"
    linked.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlink"):
        await workspace.export(linked / "nested")
    (source / "link").symlink_to(source / "fixture.bin")
    assert (
        invoke_file_helper(
            {
                "op": "export",
                "path": str(source),
                "count": 10,
                "file_limit": 100,
                "total_limit": 100,
            }
        ).returncode
        != 0
    )


@pytest.mark.asyncio
async def test_prepare_requires_pins_and_transmits_only_remote_command():
    workspace = p.RemoteWorkspace(config(), AsyncMock())
    workspace.shell = AsyncMock(return_value='{"exit_code":0}')
    workspace.write = AsyncMock()
    source = {"repo": "huggingface/accelerate", "base_sha": "a" * 40, "head_sha": "b" * 40}
    await workspace.prepare(source)
    command = workspace.shell.await_args.args[0]
    assert "git diff --binary " + "a" * 40 + " " + "b" * 40 in command
    assert "test ! -e /workspace/repo" in command
    assert "git rev-parse" in command
    workspace.write.assert_awaited_once_with("/private/pr.json", json.dumps(source, indent=2))
    with pytest.raises(ValueError, match="pinned"):
        await workspace.prepare({**source, "head_sha": "main"})


@pytest.mark.asyncio
async def test_bounded_write_chunks_fit_command_argument_and_bad_paths_never_dispatch():
    workspace = p.RemoteWorkspace(config(), AsyncMock())
    workspace._run = AsyncMock(return_value={})
    await workspace.write("/output/task/test.py", b"x" * 60_000)
    assert [c.args[1]["offset"] for c in workspace._run.await_args_list] == [0, 24000, 48000]
    workspace._run.reset_mock()
    for path in ("relative", "/../escape", "/tmp//file"):
        with pytest.raises(ValueError):
            await workspace.write(path, "x")
    with pytest.raises(ValueError, match="limit"):
        await workspace.write("/output/task/large", b"x" * 2_000_001)
    workspace._run.assert_not_awaited()
