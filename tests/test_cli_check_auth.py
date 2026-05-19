"""Smoke test for `repo2rlenv push --check-auth`."""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

from repo2rlenv import cli as cli_mod
from repo2rlenv.cli import _run_check_auth
from repo2rlenv.registry.auth import (
    CredentialSource,
    RegistryAuth,
    RegistryKind,
)
from repo2rlenv.registry.probe import ProbeResult


def _make_args(**kwargs: Any) -> argparse.Namespace:
    base = {
        "check_auth": True,
        "fast": False,
        "json": False,
        "dataset": "testorg/whatever",
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


class TestCheckAuthJSON:
    def test_emits_structured_output(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli_mod, "_run_check_auth", _run_check_auth)
        # Stub HF
        monkeypatch.setattr("repo2rlenv.auth.resolve_hf_token", lambda _a: "fake")
        # Stub discovery
        auth = RegistryAuth(
            host="ghcr.io",
            kind=RegistryKind.GHCR,
            cred_source=CredentialSource.INLINE,
        )
        monkeypatch.setattr(
            "repo2rlenv.registry.auth.discover_logged_in_registries",
            lambda config_path=None: [auth],
        )
        # Stub probe
        pr = ProbeResult(
            host="ghcr.io",
            kind=RegistryKind.GHCR,
            namespace="testorg",
            levels_checked=(1, 2, 3, 4),
            reachable=True,
            authenticated=True,
            can_read=True,
            can_write=True,
            elapsed_sec=0.123,
        )
        monkeypatch.setattr("repo2rlenv.registry.probe.probe", lambda *a, **kw: pr)

        rc = _run_check_auth(_make_args(json=True))
        out = capsys.readouterr().out
        assert rc == 0
        data = json.loads(out)
        assert data["hf_hub"]["logged_in"] is True
        assert data["registries"][0]["host"] == "ghcr.io"
        assert data["registries"][0]["is_pushable"] is True


class TestCheckAuthHumanOutput:
    def test_no_creds_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("repo2rlenv.auth.resolve_hf_token", lambda _a: None)
        monkeypatch.setattr(
            "repo2rlenv.registry.auth.discover_logged_in_registries",
            lambda config_path=None: [],
        )
        rc = _run_check_auth(_make_args(json=False))
        assert rc == 0

    def test_fast_mode_limits_levels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("repo2rlenv.auth.resolve_hf_token", lambda _a: "fake")
        auth = RegistryAuth(
            host="ghcr.io",
            kind=RegistryKind.GHCR,
            cred_source=CredentialSource.INLINE,
        )
        monkeypatch.setattr(
            "repo2rlenv.registry.auth.discover_logged_in_registries",
            lambda config_path=None: [auth],
        )
        captured_levels: list[tuple[int, ...]] = []

        def fake_probe(
            *args: Any, levels: tuple[int, ...] = (1, 2, 3, 4), **kwargs: Any
        ) -> ProbeResult:
            captured_levels.append(levels)
            return ProbeResult(
                host="ghcr.io",
                kind=RegistryKind.GHCR,
                namespace="testorg",
                levels_checked=levels,
                reachable=True,
                authenticated=True,
            )

        monkeypatch.setattr("repo2rlenv.registry.probe.probe", fake_probe)
        rc = _run_check_auth(_make_args(fast=True))
        assert rc == 0
        assert captured_levels == [(1, 2)]


class TestCmdPushDispatchesCheckAuth:
    """Verify `cmd_push --check-auth` short-circuits before touching the dataset."""

    def test_check_auth_bypasses_dataset_validation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[bool] = []

        def fake_check(args: argparse.Namespace) -> int:
            captured.append(True)
            return 0

        monkeypatch.setattr(cli_mod, "_run_check_auth", fake_check)
        args = argparse.Namespace(
            check_auth=True,
            local_dir="/nonexistent",
            dataset="",
            private=False,
            message=None,
            image_registry=None,
            inline_dockerfile=False,
            require_registry=False,
            skip_image_push=False,
            image_visibility="inherit",
            fast=False,
            json=False,
        )
        rc = cli_mod.cmd_push(args)
        assert rc == 0
        assert captured == [True]
