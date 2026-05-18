"""Tests for `registry.push` — docker push subprocess wrapper."""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

import pytest

from repo2rlenv.registry import push as push_mod
from repo2rlenv.registry.push import (
    PushError,
    _resolve_repo_digest,
    manifest_exists,
    push_image,
)


def _stub_run(
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[tuple[str, ...], tuple[int, str, str]],
) -> list[list[str]]:
    """Record subprocess.run invocations and return scripted responses.

    `responses` maps (docker, subcommand, ...) prefixes to (returncode, stdout, stderr).
    The most specific matching prefix wins.
    """
    called: list[list[str]] = []

    def fake(args: list[str], **kwargs: Any) -> mock.MagicMock:
        called.append(args)
        # Find the longest prefix match
        match: tuple[int, str, str] | None = None
        match_len = -1
        for key, value in responses.items():
            if len(key) > match_len and tuple(args[: len(key)]) == key:
                match = value
                match_len = len(key)
        if match is None:
            return mock.MagicMock(returncode=127, stdout="", stderr=f"no script: {args}")
        rc, stdout, stderr = match
        return mock.MagicMock(returncode=rc, stdout=stdout, stderr=stderr)

    monkeypatch.setattr("subprocess.run", fake)
    return called


class TestManifestExists:
    def test_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(push_mod, "_docker_available", lambda: True)
        _stub_run(monkeypatch, {("docker", "manifest", "inspect"): (0, "{}", "")})
        assert manifest_exists("ghcr.io/foo/bar:tag") is True

    def test_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(push_mod, "_docker_available", lambda: True)
        _stub_run(monkeypatch, {("docker", "manifest", "inspect"): (1, "", "manifest unknown")})
        assert manifest_exists("ghcr.io/foo/bar:tag") is False

    def test_no_docker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(push_mod, "_docker_available", lambda: False)
        assert manifest_exists("ghcr.io/foo/bar:tag") is False


class TestResolveRepoDigest:
    def test_filters_by_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        digests = json.dumps(
            [
                "docker.io/foo/bar@sha256:111",
                "ghcr.io/foo/bar@sha256:222",
            ]
        )
        _stub_run(monkeypatch, {("docker", "image", "inspect"): (0, digests, "")})
        result = _resolve_repo_digest("ghcr.io/foo/bar:tag")
        assert result == "ghcr.io/foo/bar@sha256:222"

    def test_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_run(monkeypatch, {("docker", "image", "inspect"): (0, "[]", "")})
        assert _resolve_repo_digest("ghcr.io/foo/bar:tag") is None

    def test_inspect_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_run(monkeypatch, {("docker", "image", "inspect"): (1, "", "not found")})
        assert _resolve_repo_digest("ghcr.io/foo/bar:tag") is None


class TestPushImage:
    def test_no_docker_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(push_mod, "_docker_available", lambda: False)
        with pytest.raises(PushError, match="not available"):
            push_image("local:tag", "ghcr.io/foo/bar:tag")

    def test_missing_local_image_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(push_mod, "_docker_available", lambda: True)
        _stub_run(monkeypatch, {("docker", "image", "inspect"): (1, "", "no such image")})
        with pytest.raises(PushError, match="not found"):
            push_image("local:tag", "ghcr.io/foo/bar:tag")

    def test_idempotent_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Manifest already at registry → skip the push step entirely."""
        monkeypatch.setattr(push_mod, "_docker_available", lambda: True)
        digest_json = json.dumps(["ghcr.io/foo/bar@sha256:cafe"])
        called = _stub_run(
            monkeypatch,
            {
                # image inspect → exists
                ("docker", "image", "inspect", "local:tag"): (0, "sha256:abc", ""),
                # manifest exists → 0
                ("docker", "manifest", "inspect"): (0, "{}", ""),
                # repo digest lookup
                ("docker", "image", "inspect", "ghcr.io/foo/bar:tag"): (0, digest_json, ""),
            },
        )
        result = push_image("local:tag", "ghcr.io/foo/bar:tag")
        assert result.pushed is False
        assert result.digest == "ghcr.io/foo/bar@sha256:cafe"
        # No `docker tag` or `docker push` should have been issued
        cmds = [tuple(c[:2]) for c in called]
        assert ("docker", "tag") not in cmds
        assert ("docker", "push") not in cmds

    def test_happy_push(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(push_mod, "_docker_available", lambda: True)
        digest_json = json.dumps(["ghcr.io/foo/bar@sha256:beef"])
        called = _stub_run(
            monkeypatch,
            {
                ("docker", "image", "inspect", "local:tag"): (0, "sha256:abc", ""),
                ("docker", "manifest", "inspect"): (1, "", "no such manifest"),
                ("docker", "tag"): (0, "", ""),
                ("docker", "push"): (0, "pushed", ""),
                ("docker", "image", "inspect", "ghcr.io/foo/bar:tag"): (0, digest_json, ""),
            },
        )
        result = push_image("local:tag", "ghcr.io/foo/bar:tag")
        assert result.pushed is True
        assert result.digest == "ghcr.io/foo/bar@sha256:beef"
        # Verify the sequence: inspect → manifest inspect → tag → push → inspect
        cmds = [tuple(c[:2]) for c in called]
        assert cmds[0] == ("docker", "image")
        assert ("docker", "tag") in cmds
        assert ("docker", "push") in cmds

    def test_push_failure_surfaces_stderr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(push_mod, "_docker_available", lambda: True)
        _stub_run(
            monkeypatch,
            {
                ("docker", "image", "inspect", "local:tag"): (0, "sha256:abc", ""),
                ("docker", "manifest", "inspect"): (1, "", ""),
                ("docker", "tag"): (0, "", ""),
                ("docker", "push"): (1, "", "denied: permission_denied"),
            },
        )
        with pytest.raises(PushError, match="denied"):
            push_image("local:tag", "ghcr.io/foo/bar:tag")

    def test_skip_if_exists_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When skip_if_exists=False, push proceeds even if manifest is present."""
        monkeypatch.setattr(push_mod, "_docker_available", lambda: True)
        digest_json = json.dumps(["ghcr.io/foo/bar@sha256:new"])
        called = _stub_run(
            monkeypatch,
            {
                ("docker", "image", "inspect", "local:tag"): (0, "sha256:abc", ""),
                ("docker", "manifest", "inspect"): (0, "{}", ""),  # exists
                ("docker", "tag"): (0, "", ""),
                ("docker", "push"): (0, "pushed", ""),
                ("docker", "image", "inspect", "ghcr.io/foo/bar:tag"): (0, digest_json, ""),
            },
        )
        result = push_image("local:tag", "ghcr.io/foo/bar:tag", skip_if_exists=False)
        assert result.pushed is True
        cmds = [tuple(c[:2]) for c in called]
        assert ("docker", "push") in cmds
