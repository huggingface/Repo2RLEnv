"""GitLab MR client — mocked HTTP unit tests + an opt-in live gitlab.com test."""

from __future__ import annotations

import os

import pytest

from repo2rlenv import gitlab
from repo2rlenv.github import PullRequestSummary


@pytest.fixture
def mock_api(monkeypatch):
    """Stub `gitlab._request` with canned list/changes/diff responses."""
    list_rows = [
        {
            "iid": 107,
            "title": "Fix the thing. Closes #10",
            "description": "body text",
            "merged_at": "2020-10-19T20:38:47.843Z",
            "target_branch": "master",
            "draft": False,
            "sha": "78d5a90",
            "web_url": "https://gitlab.com/o/n/-/merge_requests/107",
        },
        {"iid": 8, "title": "WIP: skip me", "draft": True, "merged_at": "2020-01-01T00:00:00Z"},
    ]
    changes = {
        "diff_refs": {"base_sha": "e8a8572", "head_sha": "78d5a90", "start_sha": "e8a8572"},
        "changes": [{"new_path": "src/foo.py"}, {"new_path": "tests/test_foo.py"}],
    }

    def fake_request(url, token, *, accept_json=True):
        if url.endswith(".diff"):
            return "diff --git a/src/foo.py b/src/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
        if "/changes" in url:
            return changes
        if "/issues/" in url:
            return {"title": "Issue title", "description": "issue body"}
        if "/merge_requests?" in url:
            return list_rows
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(gitlab, "_request", fake_request)


def test_list_merged_prs_shape_and_draft_skip(mock_api):
    prs = gitlab.list_merged_prs("o", "n", limit=10)
    assert len(prs) == 1  # the draft MR is skipped
    pr = prs[0]
    assert isinstance(pr, PullRequestSummary)
    assert pr.number == 107
    assert pr.base_sha == "e8a8572"  # from diff_refs
    assert pr.base_ref == "master"
    assert pr.changed_files == ["src/foo.py", "tests/test_foo.py"]
    assert pr.url.endswith("/merge_requests/107")


def test_fetch_pr_diff_is_git_format(mock_api):
    diff = gitlab.fetch_pr_diff("o", "n", 107)
    assert diff.startswith("diff --git a/src/foo.py b/src/foo.py")


def test_fetch_issue(mock_api):
    assert gitlab.fetch_issue("o", "n", 10) == ("Issue title", "issue body")


def test_project_id_url_encodes():
    assert (
        gitlab._project_id("python-devs", "importlib_resources")
        == "python-devs%2Fimportlib_resources"
    )


@pytest.mark.skipif(
    not os.environ.get("R2E_LIVE_GITLAB"),
    reason="set R2E_LIVE_GITLAB=1 to hit the real gitlab.com API",
)
def test_live_gitlab_mr_mining():
    prs = gitlab.list_merged_prs("python-devs", "importlib_resources", limit=2)
    assert prs and prs[0].base_sha and prs[0].changed_files
    diff = gitlab.fetch_pr_diff("python-devs", "importlib_resources", prs[0].number)
    assert diff.startswith("diff --git")
