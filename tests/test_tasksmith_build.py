from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import ClassVar

import pytest

from repo2rlenv.curation.budget import Budget
from repo2rlenv.tasksmith import bootstrap, build
from repo2rlenv.tasksmith.providers import (
    BuildSpec,
    CleanupUncertain,
    ResourceReceipt,
    WorkspaceConfig,
)


def workspace_kwargs():
    return dict(
        provider="modal",
        operation_id="test:build",
        deadline=time.time() + 600,
        build=BuildSpec(
            kind="recipe", role="bootstrap", files=(("Dockerfile", b"FROM immutable-fixture\n"),)
        ),
        network="public",
        cpus=2,
        memory_mib=4096,
    )


def test_retained_workspace_preserves_original_deadline(tmp_path):
    kwargs = workspace_kwargs()
    path = tmp_path / "workspace.json"
    first = build.workspace_config(path, **kwargs)
    before = path.read_bytes()
    assert build.workspace_config(path, **{**kwargs, "deadline": kwargs["deadline"] + 600}) == first
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="requested deadline"):
        build.workspace_config(path, **{**kwargs, "deadline": kwargs["deadline"] - 30})
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("cpus", 4),
        ("memory_mib", 8192),
        ("disk_gib", 20),
        ("network", "none"),
        ("app_name", "other"),
        ("max_file_bytes", 1_000_000),
    ],
)
def test_retained_workspace_rejects_every_material_resource_change(tmp_path, field, value):
    kwargs = workspace_kwargs()
    path = tmp_path / "workspace.json"
    build.workspace_config(path, **kwargs)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="identity changed"):
        build.workspace_config(path, **{**kwargs, field: value})
    assert path.read_bytes() == before


def test_retained_workspace_rejects_changed_build_bytes(tmp_path):
    kwargs = workspace_kwargs()
    path = tmp_path / "workspace.json"
    build.workspace_config(path, **kwargs)
    before = path.read_bytes()
    changed = BuildSpec(
        kind="recipe", role="bootstrap", files=(("Dockerfile", b"FROM different-fixture\n"),)
    )
    with pytest.raises(ValueError, match="identity changed"):
        build.workspace_config(path, **{**kwargs, "build": changed})
    assert path.read_bytes() == before


class FakeWorkspace:
    events: ClassVar[list[str]] = []
    uncertain = False
    fail_start = False

    def __init__(self, config, on_receipt):
        self.config = config
        self.on_receipt = on_receipt
        self.resource_id = None

    async def record(self, status):
        c = self.config
        row = ResourceReceipt(
            c.provider,
            c.operation_id,
            c.digest,
            c.build.digest,
            c.build.role,
            c.profile,
            c.deadline,
            self.resource_id,
            "im-fixture",
            status,
            time.time(),
            "fixture",
            c.cpus,
            c.memory_mib,
            c.disk_gib,
            0,
        )
        await self.on_receipt(row)
        return row

    async def start(self):
        self.events.append("start")
        await self.record("claimed")
        if self.fail_start:
            raise RuntimeError("unknown create result")
        self.resource_id = "sb-fixture"
        await self.record("running")

    async def attach(self, resource_id):
        self.events.append("attach")
        self.resource_id = resource_id
        await self.record("attached")

    async def stop(self):
        self.events.append("stop")
        if self.uncertain:
            await self.record("uncertain")
            raise CleanupUncertain("unknown termination")
        return await self.record("terminated")


@pytest.fixture
def fake_workspace(monkeypatch):
    FakeWorkspace.events = []
    FakeWorkspace.uncertain = False
    FakeWorkspace.fail_start = False
    monkeypatch.setattr(bootstrap, "RemoteWorkspace", FakeWorkspace)
    return FakeWorkspace


def test_workspace_settles_only_after_confirmed_cleanup(tmp_path, fake_workspace):
    budget = Budget(tmp_path / "ledger.json", 10)
    config = WorkspaceConfig(**workspace_kwargs())

    async def run():
        async with bootstrap.budgeted_workspace(config, tmp_path / "remote", budget, 2) as remote:
            assert remote.resource_id == "sb-fixture"
            assert budget.spent == 2

    asyncio.run(run())
    assert fake_workspace.events == ["start", "stop"]
    reservation = json.loads((tmp_path / "remote/reservation.json").read_text())
    assert reservation["settled"]
    assert 0 < budget.spent < 2
    assert json.loads((tmp_path / "remote/resource.json").read_text())["status"] == "terminated"


def test_uncertain_cleanup_keeps_full_reservation_and_prevents_restart(tmp_path, fake_workspace):
    fake_workspace.uncertain = True
    budget = Budget(tmp_path / "ledger.json", 10)
    config = WorkspaceConfig(**workspace_kwargs())

    async def run():
        async with bootstrap.budgeted_workspace(config, tmp_path / "remote", budget, 2):
            pass

    with pytest.raises(CleanupUncertain):
        asyncio.run(run())
    assert budget.spent == 2
    assert not json.loads((tmp_path / "remote/reservation.json").read_text())["settled"]
    with pytest.raises(RuntimeError, match="reconciliation"):
        asyncio.run(run())
    assert fake_workspace.events == ["start", "stop"]
    assert budget.spent == 2


def test_pending_creation_is_not_recreated(tmp_path, fake_workspace):
    fake_workspace.fail_start = True
    budget = Budget(tmp_path / "ledger.json", 10)
    config = WorkspaceConfig(**workspace_kwargs())

    async def run():
        async with bootstrap.budgeted_workspace(config, tmp_path / "remote", budget, 2):
            pytest.fail("Unknown creation must not enter workspace")

    with pytest.raises(RuntimeError, match="unknown create"):
        asyncio.run(run())
    with pytest.raises(RuntimeError, match="reconciliation"):
        asyncio.run(run())
    assert fake_workspace.events == ["start"]
    assert budget.spent == 2


def test_body_failure_still_terminates_and_settles(tmp_path, fake_workspace):
    budget = Budget(tmp_path / "ledger.json", 10)
    config = WorkspaceConfig(**workspace_kwargs())

    async def run():
        async with bootstrap.budgeted_workspace(config, tmp_path / "remote", budget, 2):
            raise ValueError("fixture readiness failed")

    with pytest.raises(ValueError, match="readiness failed"):
        asyncio.run(run())
    assert fake_workspace.events == ["start", "stop"]
    assert json.loads((tmp_path / "remote/reservation.json").read_text())["settled"]


def test_changed_reservation_config_fails_before_any_provider_action(tmp_path, fake_workspace):
    budget = Budget(tmp_path / "ledger.json", 10)
    config = WorkspaceConfig(**workspace_kwargs())

    async def run(cfg):
        async with bootstrap.budgeted_workspace(cfg, tmp_path / "remote", budget, 2):
            pass

    asyncio.run(run(config))
    events = fake_workspace.events[:]
    spent = budget.spent
    with pytest.raises(ValueError, match="configuration changed"):
        asyncio.run(run(replace(config, cpus=4)))
    assert fake_workspace.events == events and budget.spent == spent


def test_source_prepare_checks_actual_pinned_trees_and_retains_patch(tmp_path):
    source = {
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "base_tree_sha": "c" * 40,
        "head_tree_sha": "d" * 40,
    }
    calls = []

    class Remote:
        async def prepare(self, value):
            calls.append(("prepare", value))

        async def shell(self, command):
            calls.append(("shell", command))
            return json.dumps(
                {
                    "exit_code": 0,
                    "stdout": " ".join(
                        [source["base_tree_sha"], source["head_tree_sha"], source["base_sha"]]
                    ),
                }
            )

        async def read(self, path):
            calls.append(("read", path))
            return b"retained patch bytes; never executed"

    result = asyncio.run(build.prepare_checked(Remote(), source, tmp_path))
    assert result["trees"] == [source["base_tree_sha"], source["head_tree_sha"], source["base_sha"]]
    assert (
        result["patch_digest"] == hashlib.sha256((tmp_path / "gold.patch").read_bytes()).hexdigest()
    )
    assert [c[0] for c in calls] == ["prepare", "shell", "read"]


def test_wrong_remote_source_tree_does_not_commit_receipt(tmp_path):
    source = {
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "base_tree_sha": "c" * 40,
        "head_tree_sha": "d" * 40,
    }

    class Remote:
        async def prepare(self, value):
            pass

        async def shell(self, command):
            return json.dumps({"exit_code": 0, "stdout": "wrong trees"})

        async def read(self, path):
            pytest.fail("Mismatched tree must fail before reading reference patch")

    with pytest.raises(ValueError, match="trees differ"):
        asyncio.run(build.prepare_checked(Remote(), source, tmp_path))
    assert not (tmp_path / "source-receipt.json").exists()


def test_dependency_cache_keys_bind_recipe_and_inputs(tmp_path):
    cache = bootstrap.BootstrapCache(tmp_path)
    key = cache.identity("recipe A", {"lock": "hash1"})
    assert key != cache.identity("recipe B", {"lock": "hash1"})
    assert key != cache.identity("recipe A", {"lock": "hash2"})
    with pytest.raises(ValueError, match="confirmed ready image"):
        cache.save(
            key, "modal", "recipe A", {"lock": "hash1"}, {"status": "uncertain", "image_id": "im-x"}
        )
    assert cache.lookup(key, "modal") is None


@pytest.mark.asyncio
async def test_public_inventory_reads_complete_nul_file_from_pinned_base():
    source = {"base_sha": "a" * 40, "head_sha": "b" * 40}
    names = ["src/example/__init__.py", "docs/a file with spaces.md", "docs/café.md"]
    calls = []

    async def shell(command, timeout):
        calls.append(("shell", command, timeout))
        assert shlex.split(command) == [
            "git",
            "-C",
            "/workspace/repo",
            "ls-tree",
            "-r",
            "-z",
            "--name-only",
            source["base_sha"],
            ">",
            "/private/tasksmith-base-file-inventory",
        ]
        assert source["head_sha"] not in command
        # Tool stdout can be truncated or contain diagnostics; it is never the
        # inventory transport. Only the separately read NUL file is authoritative.
        return json.dumps({"exit_code": 0, "stdout": "TRUNCATED/invented.py\n", "stderr": ""})

    async def read(path):
        calls.append(("read", path))
        assert path == "/private/tasksmith-base-file-inventory"
        return b"\0".join(name.encode() for name in names) + b"\0"

    assert await build.public_file_inventory(
        SimpleNamespace(shell=shell, read=read), source
    ) == sorted(names)
    assert [row[0] for row in calls] == ["shell", "read"]
    assert calls[0][-1] == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [1, 2, 127, None])
async def test_public_inventory_nonzero_capture_never_reads_stale_file(exit_code):
    async def shell(*args):
        return json.dumps({"exit_code": exit_code, "stdout": "stale.py\0"})

    async def read(path):
        pytest.fail("Failed git inventory capture read a stale file")

    with pytest.raises(RuntimeError, match="pinned base file inventory"):
        await build.public_file_inventory(
            SimpleNamespace(shell=shell, read=read), {"base_sha": "a" * 40}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"src/a.py",
        b"src/a.py\0truncated",
        b"\0",
        b"a.py\0\0",
        b"a.py\0a.py\0",
        b"/outside.py\0",
        b"../outside.py\0",
        b"src/../outside.py\0",
        b".\0",
        b"src//a.py\0",
        b"src/./a.py\0",
        b"src\\a.py\0",
        b"\xff.py\0",
    ],
)
async def test_public_inventory_rejects_incomplete_or_unsafe_names(raw):
    async def shell(*args):
        return json.dumps({"exit_code": 0, "stdout": "ignored"})

    async def read(path):
        return raw

    with pytest.raises(ValueError):
        await build.public_file_inventory(
            SimpleNamespace(shell=shell, read=read), {"base_sha": "a" * 40}
        )


@pytest.mark.asyncio
async def test_public_inventory_propagates_bounded_file_read_failure():
    async def shell(*args):
        return json.dumps({"exit_code": 0, "stdout": "a.py\0"})

    async def read(path):
        raise ValueError("Remote file exceeds the bounded read limit")

    with pytest.raises(ValueError, match="bounded read limit"):
        await build.public_file_inventory(
            SimpleNamespace(shell=shell, read=read), {"base_sha": "a" * 40}
        )
