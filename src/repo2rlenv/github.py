"""Thin GitHub client built on the `gh` CLI for auth simplicity.

We deliberately shell out to `gh` rather than depend on PyGithub:
  - `gh auth token` already gives us auth resolution for free
  - `gh api graphql` is easier than maintaining REST pagination logic
  - one less Python dep

If `gh` is not installed, we fall back to plain `curl`-style requests via
`urllib`. For v0.1 we only support the `gh`-installed path.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date


class GitHubError(RuntimeError):
    pass


@dataclass(slots=True)
class PullRequestSummary:
    number: int
    title: str
    body: str
    state: str
    merged_at: str | None
    base_ref: str
    base_sha: str
    head_sha: str
    is_draft: bool
    url: str
    changed_files: list[str]


def _run_gh(args: list[str], token: str | None = None) -> str:
    if not shutil.which("gh"):
        raise GitHubError("gh CLI not found on PATH; install it or use a different auth path")
    env = None
    if token:
        import os

        env = {**os.environ, "GH_TOKEN": token}
    proc = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise GitHubError(f"gh {' '.join(args)!r} failed: {proc.stderr.strip()}")
    return proc.stdout


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
    """List recently merged PRs ordered newest-first.

    Uses `gh pr list` (REST under the hood). Filters by date client-side.
    """
    args = [
        "pr",
        "list",
        "--repo",
        f"{owner}/{name}",
        "--state",
        "merged",
        "--limit",
        str(min(limit * 3, 1000)),  # over-fetch to allow client-side filtering
        "--json",
        "number,title,body,state,mergedAt,baseRefName,baseRefOid,headRefOid,isDraft,url,files",
    ]
    raw = _run_gh(args, token=token)
    rows = json.loads(raw)

    summaries: list[PullRequestSummary] = []
    for r in rows:
        if skip_drafts and r.get("isDraft"):
            continue
        merged_at = r.get("mergedAt")
        if since and merged_at and merged_at[:10] < since.isoformat():
            continue
        if until and merged_at and merged_at[:10] > until.isoformat():
            continue
        files = [f["path"] for f in (r.get("files") or [])]
        summaries.append(
            PullRequestSummary(
                number=r["number"],
                title=r["title"] or "",
                body=r.get("body") or "",
                state=r["state"],
                merged_at=merged_at,
                base_ref=r.get("baseRefName") or "",
                base_sha=r.get("baseRefOid") or "",
                head_sha=r.get("headRefOid") or "",
                is_draft=bool(r.get("isDraft")),
                url=r["url"],
                changed_files=files,
            )
        )
        if len(summaries) >= limit:
            break
    return summaries


def fetch_pr_diff(owner: str, name: str, number: int, *, token: str | None = None) -> str:
    """Return the unified diff for a PR via `gh pr diff`."""
    return _run_gh(
        ["pr", "diff", str(number), "--repo", f"{owner}/{name}"],
        token=token,
    )


def get_primary_language(owner: str, name: str, *, token: str | None = None) -> str | None:
    """Return GitHub's primary language string for a repo, or None on failure.

    Used by the pipeline-language compatibility pre-flight check so we can
    fail fast (before bootstrap) if a Python-only pipeline is pointed at a
    Go / Rust / etc. repo. The result is GitHub Linguist's classification
    (e.g. "Python", "Go", "TypeScript"); use
    `bootstrap.language.language_from_github_name` to map it to LanguageHint.
    """
    import json as _json

    try:
        raw = _run_gh(
            ["api", f"repos/{owner}/{name}", "--jq", ".language"],
            token=token,
        ).strip()
    except GitHubError:
        return None
    if not raw or raw == "null":
        return None
    # `gh api --jq` strips quotes, but unwrap if present
    try:
        return _json.loads(raw) if raw.startswith('"') else raw
    except _json.JSONDecodeError:
        return raw


def fetch_commit_diff(owner: str, name: str, sha: str, *, token: str | None = None) -> str:
    """Return the unified diff for a single commit via `gh api`.

    Hits `GET /repos/{owner}/{repo}/commits/{sha}` with the `diff` media
    type — same shape as `git show --format= <sha>` output.
    """
    return _run_gh(
        [
            "api",
            f"repos/{owner}/{name}/commits/{sha}",
            "-H",
            "Accept: application/vnd.github.v3.diff",
        ],
        token=token,
    )


def fetch_commit_parent(owner: str, name: str, sha: str, *, token: str | None = None) -> str:
    """Return the first parent SHA of `sha` via `gh api`.

    Returns "" if the commit has no parents (root commit) or on any error.
    """
    import json as _json

    try:
        raw = _run_gh(
            ["api", f"repos/{owner}/{name}/commits/{sha}"],
            token=token,
        )
        data = _json.loads(raw)
        parents = data.get("parents", []) or []
        if not parents:
            return ""
        return parents[0].get("sha", "") or ""
    except (GitHubError, _json.JSONDecodeError):
        return ""
