"""Use Daytona's real image and request models without creating a cloud sandbox."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from repo2rlenv.execution.base import WorkerSpec
from repo2rlenv.execution.daytona import DaytonaWorker


@pytest.mark.parametrize("image", [None, "ubuntu:24.04"])
def test_worker_request_matches_installed_sdk(monkeypatch, image):
    daytona = pytest.importorskip("daytona")
    sandbox = SimpleNamespace(id="sdk-contract")
    calls = []

    def create(params, *, timeout):
        calls.append(params)
        assert isinstance(params, daytona.CreateSandboxFromImageParams)
        assert isinstance(params.resources, daytona.Resources)
        assert params.resources.memory == 5
        assert params.os_user == "root"
        assert params.ephemeral is True
        assert params.auto_stop_interval == 10
        assert params.auto_delete_interval == 0
        assert timeout == 180
        if image is None:
            assert isinstance(params.image, daytona.Image)
        else:
            assert params.image == image
        return sandbox

    client = SimpleNamespace(create=create)
    monkeypatch.setattr(daytona, "Daytona", lambda: client)
    worker = DaytonaWorker.create(
        WorkerSpec(
            provider="daytona", name="sdk-contract", memory_mb=4097, timeout_sec=600, image=image
        )
    )
    assert worker.client is client and worker.sandbox is sandbox
    assert len(calls) == 1
