"""Runner-level fixes from PR #2 review (codex P1/P2)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from repo2rlenv.bootstrap import runner
from repo2rlenv.bootstrap.runner import (
    BootstrapError,
    _scrub_token,
    _shallow_clone_at_ref,
    _resolve_repo_digest,
)


def test_scrub_token_replaces_secret():
    msg = "fatal: could not read from https://x-access-token:ghp_secret@github.com/..."
    assert "ghp_secret" not in _scrub_token(msg, "ghp_secret")
    assert "***" in _scrub_token(msg, "ghp_secret")


def test_scrub_token_passthrough_when_no_token():
    msg = "fatal: nothing"
    assert _scrub_token(msg, None) == msg


def test_shallow_clone_head_uses_plain_clone(tmp_path: Path):
    """ref='HEAD' must NOT pass --branch (it'd be interpreted as a branch named 'HEAD')."""
    with mock.patch("subprocess.run") as run:
        run.return_value = mock.Mock(returncode=0, stderr="", stdout="")
        _shallow_clone_at_ref(
            "https://github.com/owner/name", "HEAD", None, tmp_path / "out",
        )
        args = run.call_args_list[0].args[0]
        assert "--branch" not in args


def test_shallow_clone_branch_tries_clone_branch_first(tmp_path: Path):
    with mock.patch("subprocess.run") as run:
        run.return_value = mock.Mock(returncode=0, stderr="", stdout="")
        _shallow_clone_at_ref(
            "https://github.com/owner/name", "release-1.0", None, tmp_path / "out",
        )
        args = run.call_args_list[0].args[0]
        assert "--branch" in args
        idx = args.index("--branch")
        assert args[idx + 1] == "release-1.0"


def test_shallow_clone_falls_back_to_fetch_on_sha(tmp_path: Path):
    """When --branch <sha> fails, fall back to clone-no-checkout + fetch + checkout."""
    call_sequence = []

    def fake_run(cmd, **kwargs):
        call_sequence.append(cmd)
        # First call (clone --branch) fails with 128 (typical for SHA)
        if "--branch" in cmd:
            return mock.Mock(returncode=128, stderr="not found", stdout="")
        # Subsequent calls succeed
        return mock.Mock(returncode=0, stderr="", stdout="")

    with mock.patch("subprocess.run", side_effect=fake_run):
        _shallow_clone_at_ref(
            "https://github.com/owner/name",
            "a1b2c3d4e5f6",
            None,
            tmp_path / "out",
        )
    # Should have: clone --branch (failed), clone --no-checkout, fetch, checkout
    all_args = [arg for cmd in call_sequence for arg in cmd]
    assert "--branch" in all_args, "should have attempted clone --branch first"
    assert "fetch" in all_args, "fallback should `git fetch origin <ref>`"
    assert "checkout" in all_args, "fallback should `git checkout <ref>`"


def test_resolve_repo_digest_parses_inspect_output():
    """Should return the first RepoDigests entry post-push."""
    inspect_out = '["ghcr.io/owner/foo@sha256:abc123"]'
    with mock.patch.object(runner, "_run") as run:
        run.return_value = mock.Mock(ok=True, stdout=inspect_out)
        digest = _resolve_repo_digest("ghcr.io/owner/foo:abc")
    assert digest == "ghcr.io/owner/foo@sha256:abc123"


def test_resolve_repo_digest_returns_none_when_unpushed():
    """No RepoDigests yet → returns None so caller keeps the local Id."""
    with mock.patch.object(runner, "_run") as run:
        run.return_value = mock.Mock(ok=True, stdout="[]")
        assert _resolve_repo_digest("local/foo:bar") is None


def test_resolve_repo_digest_returns_none_on_inspect_fail():
    with mock.patch.object(runner, "_run") as run:
        run.return_value = mock.Mock(ok=False, stdout="")
        assert _resolve_repo_digest("missing:tag") is None


def test_user_dockerfile_missing_path_raises(tmp_path: Path):
    """Pointing user_dockerfile at a non-existent file is a clear error."""
    from repo2rlenv.spec.input import AuthSpec, BootstrapSpec, LLMSpec, RepoSpec

    repo = RepoSpec(url="owner/name", access="public")
    spec = BootstrapSpec(user_dockerfile=tmp_path / "does-not-exist.Dockerfile")
    llm = LLMSpec(provider="anthropic", model="claude-sonnet-4-6")

    # Stub out the bits that would otherwise fail before we hit the dockerfile check
    with mock.patch.object(runner, "is_docker_available", return_value=True), \
         mock.patch.object(runner, "_shallow_clone_at_ref"), \
         mock.patch.object(runner, "_resolve_head_sha", return_value="a" * 40):
        with pytest.raises(BootstrapError, match="user_dockerfile not found"):
            runner.ensure_bootstrap(repo, spec, llm, AuthSpec())
