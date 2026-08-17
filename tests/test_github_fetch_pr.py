"""`github.fetch_pr` — the by-number PR fetch used by `pr_to_env`.

The `gh` CLI is stubbed at the `_run_gh` boundary (same seam the rest of the
module funnels through), so these run offline with no `gh` installed.
"""

from __future__ import annotations

import json

import pytest

from repo2rlenv import github
from repo2rlenv.github import GitHubError, PullRequestSummary

_PR_ROW = {
    "number": 3083,
    "title": "Fix crash on empty input",
    "body": "The CLI crashes. Closes #10",
    "state": "MERGED",
    "mergedAt": "2026-01-01T00:00:00Z",
    "baseRefName": "main",
    "headRefOid": "b" * 40,
    "isDraft": False,
    "url": "https://github.com/huggingface/peft/pull/3083",
    "files": [{"path": "src/pkg/core.py"}, {"path": "tests/test_core.py"}],
}


@pytest.fixture
def mock_gh(monkeypatch):
    """Stub `github._run_gh`, recording every argv it's handed."""
    calls: list[list[str]] = []

    def fake_run_gh(args, token=None):
        calls.append(args)
        if args[:2] == ["pr", "view"]:
            return json.dumps(_PR_ROW)
        if args[0] == "api" and args[-1] == ".base.sha":
            return "a" * 40 + "\n"
        raise AssertionError(f"unexpected gh invocation: {args}")

    monkeypatch.setattr(github, "_run_gh", fake_run_gh)
    return calls


def test_fetch_pr_returns_full_summary(mock_gh):
    pr = github.fetch_pr("huggingface", "peft", 3083)
    assert isinstance(pr, PullRequestSummary)
    assert pr.number == 3083
    assert pr.title == "Fix crash on empty input"
    assert pr.body == "The CLI crashes. Closes #10"
    assert pr.state == "MERGED"
    assert pr.merged_at == "2026-01-01T00:00:00Z"
    assert pr.base_ref == "main"
    assert pr.base_sha == "a" * 40  # from the REST base-sha call, not pr view
    assert pr.head_sha == "b" * 40
    assert pr.is_draft is False
    assert pr.url.endswith("/pull/3083")
    assert pr.changed_files == ["src/pkg/core.py", "tests/test_core.py"]


def test_fetch_pr_uses_gh_pr_view_with_repo_and_json(mock_gh):
    github.fetch_pr("huggingface", "peft", 3083)
    view = mock_gh[0]
    assert view[:3] == ["pr", "view", "3083"]
    assert "--repo" in view and "huggingface/peft" in view
    # Same field set as the list path so both build an identical shape.
    assert github._PR_JSON_FIELDS in view
    # The base SHA needs a second call — `gh pr view` has no baseRefOid.
    assert any(a[0] == "api" for a in mock_gh)


def test_fetch_pr_raises_when_base_sha_unresolvable(monkeypatch):
    def fake_run_gh(args, token=None):
        if args[:2] == ["pr", "view"]:
            return json.dumps(_PR_ROW)
        raise GitHubError("gh api failed: not found")

    monkeypatch.setattr(github, "_run_gh", fake_run_gh)
    with pytest.raises(GitHubError, match="base commit SHA"):
        github.fetch_pr("huggingface", "peft", 3083)


def test_fetch_pr_raises_on_non_json(monkeypatch):
    monkeypatch.setattr(github, "_run_gh", lambda args, token=None: "not json at all")
    with pytest.raises(GitHubError, match="non-JSON"):
        github.fetch_pr("huggingface", "peft", 3083)


def test_fetch_pr_propagates_gh_failure(monkeypatch):
    def boom(args, token=None):
        raise GitHubError("gh 'pr view' failed: could not resolve to a PullRequest")

    monkeypatch.setattr(github, "_run_gh", boom)
    with pytest.raises(GitHubError):
        github.fetch_pr("huggingface", "peft", 999999)


def test_fetch_pr_forwards_token(monkeypatch):
    seen: list[str | None] = []

    def fake_run_gh(args, token=None):
        seen.append(token)
        if args[:2] == ["pr", "view"]:
            return json.dumps(_PR_ROW)
        return "a" * 40

    monkeypatch.setattr(github, "_run_gh", fake_run_gh)
    github.fetch_pr("o", "n", 1, token="ghp_secret")
    assert seen == ["ghp_secret", "ghp_secret"]


def test_list_merged_prs_still_builds_same_shape(monkeypatch):
    """Regression guard for the shared `_summary_from_pr_json` refactor."""

    def fake_run_gh(args, token=None):
        if args[:2] == ["pr", "list"]:
            return json.dumps([_PR_ROW])
        if args[:2] == ["pr", "view"]:
            return json.dumps(_PR_ROW)
        return "a" * 40

    monkeypatch.setattr(github, "_run_gh", fake_run_gh)
    (pr,) = github.list_merged_prs("huggingface", "peft", limit=1)
    assert pr == github.fetch_pr("huggingface", "peft", 3083)


def test_both_providers_expose_fetch_pr():
    """provider.py dispatches by attribute — the surface must match."""
    from repo2rlenv import gitlab
    from repo2rlenv.provider import provider_for
    from repo2rlenv.spec.input import RepoSpec

    for mod in (github, gitlab):
        assert callable(getattr(mod, "fetch_pr", None)), f"{mod.__name__}.fetch_pr missing"

    assert provider_for(RepoSpec(url="huggingface/peft")) is github
    assert provider_for(RepoSpec(url="https://gitlab.com/foo/bar")) is gitlab
