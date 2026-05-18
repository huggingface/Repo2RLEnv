"""Tests for ECR / GAR repository pre-creation helpers."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from repo2rlenv.registry import ecr as ecr_mod
from repo2rlenv.registry import gar as gar_mod
from repo2rlenv.registry.ecr import ECRError, _parse_ecr_ref, ensure_ecr_repository
from repo2rlenv.registry.gar import GARError, _parse_gar_ref, ensure_gar_repository


class TestParseEcrRef:
    def test_private(self) -> None:
        out = _parse_ecr_ref("123456789.dkr.ecr.us-east-1.amazonaws.com/r2e/foo:tag")
        assert out == ("us-east-1", "r2e/foo", False)

    def test_public(self) -> None:
        out = _parse_ecr_ref("public.ecr.aws/myalias/foo:tag")
        assert out == ("myalias", "foo", True)

    def test_non_ecr_returns_none(self) -> None:
        assert _parse_ecr_ref("ghcr.io/u/n:tag") is None

    def test_no_path_returns_none(self) -> None:
        assert _parse_ecr_ref("123.dkr.ecr.eu-west-1.amazonaws.com") is None


class TestEnsureEcrRepository:
    def test_no_aws_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ecr_mod, "_aws_available", lambda: False)
        with pytest.raises(ECRError, match="aws CLI not available"):
            ensure_ecr_repository("123.dkr.ecr.us-east-1.amazonaws.com/r2e:tag")

    def test_already_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ecr_mod, "_aws_available", lambda: True)

        def fake_run(args: list[str], **kwargs: Any) -> mock.MagicMock:
            return mock.MagicMock(returncode=0, stdout='{"repositories":[{}]}', stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)
        result = ensure_ecr_repository("123.dkr.ecr.us-east-1.amazonaws.com/r2e/foo:tag")
        assert result.created is False
        assert result.repo == "r2e/foo"
        assert result.is_public is False

    def test_creates_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ecr_mod, "_aws_available", lambda: True)
        call_count = {"n": 0}

        def fake_run(args: list[str], **kwargs: Any) -> mock.MagicMock:
            call_count["n"] += 1
            if "describe-repositories" in args:
                return mock.MagicMock(
                    returncode=255, stdout="", stderr="RepositoryNotFoundException"
                )
            if "create-repository" in args:
                return mock.MagicMock(returncode=0, stdout="{}", stderr="")
            return mock.MagicMock(returncode=127, stdout="", stderr="unscripted")

        monkeypatch.setattr("subprocess.run", fake_run)
        result = ensure_ecr_repository("123.dkr.ecr.us-east-1.amazonaws.com/r2e:tag")
        assert result.created is True
        assert call_count["n"] == 2  # describe + create

    def test_public_routing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ecr_mod, "_aws_available", lambda: True)
        captured: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: Any) -> mock.MagicMock:
            captured.append(args)
            return mock.MagicMock(returncode=0, stdout='{"repositories":[{}]}', stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)
        ensure_ecr_repository("public.ecr.aws/alias1/foo:tag")
        # Should use ecr-public subcommand, not ecr
        assert "ecr-public" in captured[0]


class TestParseGarRef:
    def test_basic(self) -> None:
        out = _parse_gar_ref("us-central1-docker.pkg.dev/myproj/r2e/img:tag")
        assert out == ("us-central1", "myproj", "r2e", "img")

    def test_with_digest(self) -> None:
        out = _parse_gar_ref("asia-southeast1-docker.pkg.dev/p/r/i@sha256:abc")
        assert out == ("asia-southeast1", "p", "r", "i")

    def test_not_gar_returns_none(self) -> None:
        assert _parse_gar_ref("ghcr.io/u/n:tag") is None


class TestEnsureGarRepository:
    def test_no_gcloud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gar_mod, "_gcloud_available", lambda: False)
        with pytest.raises(GARError, match="gcloud CLI not available"):
            ensure_gar_repository("us-central1-docker.pkg.dev/p/r/i:tag")

    def test_already_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gar_mod, "_gcloud_available", lambda: True)

        def fake_run(args: list[str], **kwargs: Any) -> mock.MagicMock:
            return mock.MagicMock(
                returncode=0, stdout="projects/p/locations/c/repositories/r", stderr=""
            )

        monkeypatch.setattr("subprocess.run", fake_run)
        result = ensure_gar_repository("us-central1-docker.pkg.dev/p/r/i:tag")
        assert result.created is False

    def test_creates_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gar_mod, "_gcloud_available", lambda: True)

        def fake_run(args: list[str], **kwargs: Any) -> mock.MagicMock:
            if "describe" in args:
                return mock.MagicMock(returncode=1, stdout="", stderr="not found")
            return mock.MagicMock(returncode=0, stdout="Created.", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)
        result = ensure_gar_repository("us-central1-docker.pkg.dev/p/r/i:tag")
        assert result.created is True
