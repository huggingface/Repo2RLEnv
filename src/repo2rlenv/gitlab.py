"""GitLab merge-request client — mirrors the `github.py` surface.

Exposes `list_merged_prs` / `fetch_pr_diff` / `fetch_issue` with the SAME
signatures and `PullRequestSummary` return type as `github.py`, so the
PR-mining pipelines (`pr_diff`, `pr_runtime`) can run against gitlab.com via
the source dispatch in `provider.py`. GitLab "merge requests" map onto our
PR abstraction (a merged MR == a merged PR; `iid` == PR number).

Pure stdlib (`urllib`) — no extra dependency, no CLI. Public projects need
no token; private ones read a `PRIVATE-TOKEN` (resolved from `$GITLAB_TOKEN`).
Scoped to gitlab.com (see #62); self-hosted instances are out of scope.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from repo2rlenv.github import PullRequestSummary

GITLAB_HOST = "https://gitlab.com"
GITLAB_API = f"{GITLAB_HOST}/api/v4"


class GitLabError(RuntimeError):
    pass


def _project_id(owner: str, name: str) -> str:
    """URL-encoded `namespace/project` path GitLab accepts as a project id."""
    return urllib.parse.quote(f"{owner}/{name}", safe="")


def _request(url: str, token: str | None, *, accept_json: bool = True):
    req = urllib.request.Request(url)
    if token:
        req.add_header("PRIVATE-TOKEN", token)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise GitLabError(f"GitLab API {url} → HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise GitLabError(f"GitLab API {url} unreachable: {exc.reason}") from exc
    return json.loads(raw) if accept_json else raw


def list_merged_prs(
    owner: str,
    name: str,
    *,
    limit: int = 50,
    since: date | None = None,
    until: date | None = None,
    skip_drafts: bool = True,
    token: str | None = None,
) -> list[PullRequestSummary]:
    """List recently merged MRs newest-first (mirrors github.list_merged_prs)."""
    pid = _project_id(owner, name)
    per_page = min(limit * 3, 100)
    rows = _request(
        f"{GITLAB_API}/projects/{pid}/merge_requests"
        f"?state=merged&order_by=created_at&sort=desc&per_page={per_page}",
        token,
    )

    summaries: list[PullRequestSummary] = []
    for r in rows:
        if skip_drafts and r.get("draft"):
            continue
        merged_at = r.get("merged_at")
        if since and merged_at and merged_at[:10] < since.isoformat():
            continue
        if until and merged_at and merged_at[:10] > until.isoformat():
            continue
        iid = r["iid"]
        # `/changes` gives diff_refs.base_sha (the merge-base the task checks
        # out at) plus the changed-file list in one call.
        try:
            changes = _request(f"{GITLAB_API}/projects/{pid}/merge_requests/{iid}/changes", token)
        except GitLabError:
            continue
        refs = changes.get("diff_refs") or {}
        files = [c["new_path"] for c in changes.get("changes", []) if c.get("new_path")]
        summaries.append(
            PullRequestSummary(
                number=iid,
                title=r.get("title") or "",
                body=r.get("description") or "",
                state="merged",
                merged_at=merged_at,
                base_ref=r.get("target_branch") or "",
                base_sha=refs.get("base_sha") or "",
                head_sha=refs.get("head_sha") or r.get("sha") or "",
                is_draft=bool(r.get("draft")),
                url=r.get("web_url") or "",
                changed_files=files,
            )
        )
        if len(summaries) >= limit:
            break
    return summaries


def fetch_pr_diff(owner: str, name: str, number: int, *, token: str | None = None) -> str:
    """Return the MR's unified diff via the `<mr>.diff` endpoint (git-format)."""
    url = f"{GITLAB_HOST}/{owner}/{name}/-/merge_requests/{number}.diff"
    return _request(url, token, accept_json=False)


def fetch_issue(
    owner: str, name: str, number: int, *, token: str | None = None
) -> tuple[str, str] | None:
    """Return (title, description) for a GitLab issue, or None if unavailable."""
    try:
        issue = _request(f"{GITLAB_API}/projects/{_project_id(owner, name)}/issues/{number}", token)
    except GitLabError:
        return None
    return issue.get("title") or "", issue.get("description") or ""
