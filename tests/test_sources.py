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
    gh = capabilities_for(SourceKind.GITHUB)
    assert {Capability.PULL_REQUESTS, Capability.ISSUES, Capability.COMMIT_API} <= gh
    # GitLab: MRs + issues via REST, but NOT commit_api (cve_patches is OSV/GitHub).
    assert capabilities_for(SourceKind.GITLAB) == frozenset(
        {Capability.PULL_REQUESTS, Capability.ISSUES}
    )
    assert capabilities_for(SourceKind.LOCAL) == frozenset()


def test_issues_capability_present_on_remotes_only():
    """commit_runtime gates issue enrichment on this — a *local* commit with
    `Closes #N` must not trigger any host API call."""
    assert Capability.ISSUES in capabilities_for(SourceKind.GITHUB)
    assert Capability.ISSUES in capabilities_for(SourceKind.GITLAB)
    assert Capability.ISSUES not in capabilities_for(SourceKind.LOCAL)


def test_detect_source_kind():
    assert detect_source_kind("https://github.com/a/b") == SourceKind.GITHUB
    assert detect_source_kind("https://gitlab.com/a/b") == SourceKind.GITLAB
    assert detect_source_kind("file:///tmp/a") == SourceKind.LOCAL


def _allowed(name: str, kind: SourceKind) -> bool:
    req = getattr(PIPELINES[name], "required_capabilities", frozenset())
    return req <= capabilities_for(kind)


def test_gating_contract_per_source():
    """LOCAL → only git/source pipelines. GITLAB → also PR pipelines (MR mining),
    but NOT cve_patches (no commit_api). GITHUB → everything."""
    git_only = ("commit_runtime", "code_instruct", "equivalence_tests")
    pr_based = ("pr_diff", "pr_runtime")

    # local: PR + CVE pipelines all blocked; git/source allowed
    for n in (*pr_based, "cve_patches"):
        assert not _allowed(n, SourceKind.LOCAL)
    for n in git_only:
        assert _allowed(n, SourceKind.LOCAL)

    # gitlab: PR pipelines now allowed (MR mining), cve_patches still blocked
    for n in pr_based:
        assert _allowed(n, SourceKind.GITLAB)
    assert not _allowed("cve_patches", SourceKind.GITLAB)

    # github: all allowed
    for n in PIPELINES:
        assert _allowed(n, SourceKind.GITHUB)


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
