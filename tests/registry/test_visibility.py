"""Tests for `registry.visibility` — GHCR package visibility flips."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from repo2rlenv.registry import visibility as vis_mod
from repo2rlenv.registry.visibility import (
    _parse_ghcr_package,
    ensure_ghcr_visibility,
)


class TestParseGhcrPackage:
    def test_tag_ref(self) -> None:
        assert _parse_ghcr_package("ghcr.io/huggingface/r2e-bootstrap:tag") == (
            "huggingface",
            "r2e-bootstrap",
        )

    def test_digest_ref(self) -> None:
        assert _parse_ghcr_package("ghcr.io/u/n@sha256:abc") == ("u", "n")

    def test_not_ghcr_returns_none(self) -> None:
        assert _parse_ghcr_package("docker.io/u/n:tag") is None

    def test_malformed_returns_none(self) -> None:
        assert _parse_ghcr_package("ghcr.io/just-one-segment:tag") is None


class TestEnsureGhcrVisibility:
    def test_not_a_ghcr_ref(self) -> None:
        result = ensure_ghcr_visibility("docker.io/u/n:tag")
        assert result.success is False
        assert "not a ghcr.io ref" in (result.error or "")

    def test_no_gh_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(vis_mod, "_gh_available", lambda: False)
        result = ensure_ghcr_visibility("ghcr.io/u/n:tag")
        assert result.success is False
        assert result.manual_url and "github.com" in result.manual_url

    def test_user_namespace_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(vis_mod, "_gh_available", lambda: True)
        monkeypatch.setattr(vis_mod, "_gh_user", lambda: "alice")

        captured: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: Any) -> mock.MagicMock:
            captured.append(args)
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)
        result = ensure_ghcr_visibility("ghcr.io/alice/myimg:tag", target="public")
        assert result.success
        # Should hit /user/... endpoint, not /orgs/...
        assert any("/user/packages/container/myimg" in arg for arg in captured[0])
        assert any("visibility=public" in arg for arg in captured[0])

    def test_org_namespace_routing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(vis_mod, "_gh_available", lambda: True)
        monkeypatch.setattr(vis_mod, "_gh_user", lambda: "alice")

        captured: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: Any) -> mock.MagicMock:
            captured.append(args)
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)
        result = ensure_ghcr_visibility("ghcr.io/bigorg/myimg:tag")
        assert result.success
        assert any("/orgs/bigorg/packages/container/myimg" in arg for arg in captured[0])

    def test_api_failure_includes_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(vis_mod, "_gh_available", lambda: True)
        monkeypatch.setattr(vis_mod, "_gh_user", lambda: "alice")

        def fake_run(args: list[str], **kwargs: Any) -> mock.MagicMock:
            return mock.MagicMock(
                returncode=1,
                stdout="",
                stderr='{"message": "package not found"}',
            )

        monkeypatch.setattr("subprocess.run", fake_run)
        result = ensure_ghcr_visibility("ghcr.io/bigorg/myimg:tag")
        assert result.success is False
        # Either the parsed message or the raw stderr — both fine
        assert "package not found" in (result.error or "") or "package not found" in (
            result.error or ""
        )
        assert result.manual_url and "github.com" in result.manual_url
