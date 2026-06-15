"""Input-source abstraction: detection, capabilities, gating, local clone."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from repo2rlenv.auth import auth_clone_url, resolve_repo_token
from repo2rlenv.pipelines import PIPELINES
from repo2rlenv.sources import (
    Capability,
    SourceKind,
    capabilities_for,
    detect_source_kind,
)
from repo2rlenv.spec.input import AuthSpec, RepoSpec

# ---------------------------------------------------------------------------
# RepoSpec normalization + source_kind + owner_name
# ---------------------------------------------------------------------------


def test_bare_owner_name_still_github():
    """Backwards compat: bare owner/name resolves to GitHub exactly as before."""
    r = RepoSpec(url="huggingface/trl")
    assert r.url == "https://github.com/huggingface/trl"
    assert r.source_kind == SourceKind.GITHUB
    assert r.owner_name == ("huggingface", "trl")


def test_github_full_url_unchanged():
    r = RepoSpec(url="https://github.com/pallets/click.git")
    assert r.url == "https://github.com/pallets/click"
    assert r.source_kind == SourceKind.GITHUB


def test_gitlab_url_detected():
    r = RepoSpec(url="https://gitlab.com/python-devs/importlib_resources")
    assert r.source_kind == SourceKind.GITLAB
    assert r.owner_name == ("python-devs", "importlib_resources")


def test_local_path_canonicalized_to_file_url(tmp_path):
    r = RepoSpec(url=str(tmp_path))
    assert r.url.startswith("file://")
    assert r.source_kind == SourceKind.LOCAL
    assert r.owner_name == ("local", tmp_path.name)


def test_file_url_and_relative_path_are_local():
    assert RepoSpec(url="file:///tmp/x").source_kind == SourceKind.LOCAL
    assert RepoSpec(url="./somedir").source_kind == SourceKind.LOCAL


def test_bare_single_token_still_rejected():
    with pytest.raises(ValueError):
        RepoSpec(url="not-a-repo")


# ---------------------------------------------------------------------------
# Capabilities + gating
# ---------------------------------------------------------------------------


def test_capabilities_by_source():
    assert Capability.PULL_REQUESTS in capabilities_for(SourceKind.GITHUB)
    assert capabilities_for(SourceKind.LOCAL) == frozenset()
    assert capabilities_for(SourceKind.GITLAB) == frozenset()


def test_detect_source_kind():
    assert detect_source_kind("https://github.com/a/b") == SourceKind.GITHUB
    assert detect_source_kind("https://gitlab.com/a/b") == SourceKind.GITLAB
    assert detect_source_kind("file:///tmp/a") == SourceKind.LOCAL


def test_pr_pipelines_blocked_on_local_and_gitlab():
    """The gating contract: PR/CVE pipelines need caps local/gitlab lack;
    the git/source pipelines need none."""
    needs_caps = {"pr_diff", "pr_runtime", "cve_patches"}
    git_only = {"commit_runtime", "code_instruct", "equivalence_tests"}
    for kind in (SourceKind.LOCAL, SourceKind.GITLAB):
        avail = capabilities_for(kind)
        for n in needs_caps:
            req = getattr(PIPELINES[n], "required_capabilities", frozenset())
            assert not (req <= avail), f"{n} should be blocked on {kind}"
        for n in git_only:
            req = getattr(PIPELINES[n], "required_capabilities", frozenset())
            assert req <= avail, f"{n} should run on {kind}"


def test_all_pipelines_allowed_on_github():
    avail = capabilities_for(SourceKind.GITHUB)
    for n, cls in PIPELINES.items():
        req = getattr(cls, "required_capabilities", frozenset())
        assert req <= avail, f"{n} should run on github"


# ---------------------------------------------------------------------------
# Token resolution + clone URL
# ---------------------------------------------------------------------------


def test_resolve_token_local_is_none(tmp_path):
    assert resolve_repo_token(RepoSpec(url=str(tmp_path)), AuthSpec()) is None


def test_resolve_token_gitlab_env(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xyz")
    r = RepoSpec(url="https://gitlab.com/a/b")
    assert resolve_repo_token(r, AuthSpec()) == "glpat-xyz"


def test_clone_url_injection():
    assert (
        auth_clone_url("https://github.com/a/b", "t") == "https://x-access-token:t@github.com/a/b"
    )
    assert auth_clone_url("https://gitlab.com/a/b", "t") == "https://oauth2:t@gitlab.com/a/b"
    assert auth_clone_url("file:///tmp/x", "t") == "file:///tmp/x"  # local untouched


# ---------------------------------------------------------------------------
# Local clone round-trip (the core of #59)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_local_repo_clones(tmp_path):
    """A local git repo resolves to file:// and clones via _shallow_clone_at_ref."""
    from repo2rlenv.bootstrap.runner import _shallow_clone_at_ref

    origin = tmp_path / "origin"
    origin.mkdir()
    run = lambda *a: subprocess.run(a, cwd=origin, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (origin / "hello.py").write_text("x = 1\n")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "init")

    repo = RepoSpec(url=str(origin))
    assert repo.url == f"file://{origin}"
    token = resolve_repo_token(repo, AuthSpec())  # None for local
    dest = tmp_path / "clone"
    _shallow_clone_at_ref(repo.url, "main", token, dest)
    assert (dest / "hello.py").read_text() == "x = 1\n"
