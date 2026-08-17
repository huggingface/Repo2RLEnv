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
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)


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


def _fetch_base_sha(owner: str, name: str, number: int, *, token: str | None = None) -> str | None:
    """
    Return a PR's base-branch commit SHA via the REST pulls endpoint, or None on failure.
    """
    try:
        raw = _run_gh(
            ["api", f"repos/{owner}/{name}/pulls/{number}", "--jq", ".base.sha"],
            token=token,
        )
    except GitHubError as exc:
        logger.warning("PR #%d: base_sha fetch failed, dropping: %s", number, exc)
        return None
    sha = raw.strip()
    if not sha:
        logger.warning("PR #%d: base_sha fetch returned empty, dropping", number)
        return None
    return sha


# The `--json` field set both `gh pr list` and `gh pr view` are asked for.
# Keep them identical so `list_merged_prs` and `fetch_pr` build the same shape.
_PR_JSON_FIELDS = "number,title,body,state,mergedAt,baseRefName,headRefOid,isDraft,url,files"


def _summary_from_pr_json(row: dict, base_sha: str) -> PullRequestSummary:
    """Build a PullRequestSummary from one `gh pr {list,view} --json ...` row."""
    return PullRequestSummary(
        number=row["number"],
        title=row.get("title") or "",
        body=row.get("body") or "",
        state=row.get("state") or "",
        merged_at=row.get("mergedAt"),
        base_ref=row.get("baseRefName") or "",
        base_sha=base_sha,
        head_sha=row.get("headRefOid") or "",
        is_draft=bool(row.get("isDraft")),
        url=row.get("url") or "",
        changed_files=[f["path"] for f in (row.get("files") or []) if f.get("path")],
    )


def fetch_pr(owner: str, name: str, number: int, *, token: str | None = None) -> PullRequestSummary:
    """Return a single PR by number via `gh pr view`.

    The by-number counterpart to `list_merged_prs`, for import-shape pipelines
    (`pr_to_env`) that are handed curated PR URLs instead of mining history.
    Unlike the list path — which silently drops rows it can't complete — this
    raises `GitHubError` on any failure, because the caller asked for *this*
    PR specifically and a silent None would become a confusing crash later.
    """
    raw = _run_gh(
        ["pr", "view", str(number), "--repo", f"{owner}/{name}", "--json", _PR_JSON_FIELDS],
        token=token,
    )
    try:
        row = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitHubError(f"{owner}/{name}#{number}: gh returned non-JSON: {raw[:200]!r}") from exc
    # `gh pr view` has no baseRefOid field, so the base SHA needs the REST call.
    base_sha = _fetch_base_sha(owner, name, number, token=token)
    if not base_sha:
        raise GitHubError(f"{owner}/{name}#{number}: could not resolve base commit SHA")
    return _summary_from_pr_json(row, base_sha)


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
        _PR_JSON_FIELDS,
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
        base_sha = _fetch_base_sha(owner, name, r["number"], token=token)
        if base_sha is None:
            continue
        summaries.append(_summary_from_pr_json(r, base_sha))
        if len(summaries) >= limit:
            break
    return summaries


def fetch_pr_diff(owner: str, name: str, number: int, *, token: str | None = None) -> str:
    """Return the unified diff for a PR via `gh pr diff`."""
    return _run_gh(
        ["pr", "diff", str(number), "--repo", f"{owner}/{name}"],
        token=token,
    )


def fetch_issue(
    owner: str, name: str, number: int, *, token: str | None = None
) -> tuple[str, str] | None:
    """Return (title, body) for an issue, or None if it can't be fetched.

    Used by `pr_runtime` to source the problem statement from the linked
    issue (the bug *report*) rather than the PR body (the *fix* description,
    which routinely leaks the solution — commit SHAs, the approach, even the
    grading test names). Mirrors SWE-bench, which builds problem statements
    from issue text.

    `gh issue view` also resolves issue numbers that are actually PRs on the
    same repo; we tolerate that and just return whatever title/body comes
    back. Returns None on any error (issue deleted, cross-repo ref, etc.) so
    the caller can fall back to the PR body.
    """
    import json as _json

    try:
        raw = _run_gh(
            ["issue", "view", str(number), "--repo", f"{owner}/{name}", "--json", "title,body"],
            token=token,
        )
        data = _json.loads(raw)
    except (GitHubError, _json.JSONDecodeError):
        return None
    title = (data.get("title") or "").strip()
    body = (data.get("body") or "").strip()
    if not title and not body:
        return None
    return title, body


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


def fetch_file_at_ref(
    owner: str, name: str, path: str, ref: str, *, token: str | None = None
) -> str | None:
    """Return a file's raw text content at a given ref, or None on failure.

    Hits `GET /repos/{owner}/{repo}/contents/{path}?ref={ref}` with the raw
    media type. Used to give an LLM the full pre-fix source when synthesizing
    a regression test (the diff alone lacks imports + surrounding code).
    """
    try:
        return _run_gh(
            [
                "api",
                f"repos/{owner}/{name}/contents/{path}?ref={ref}",
                "-H",
                "Accept: application/vnd.github.raw",
            ],
            token=token,
        )
    except GitHubError:
        return None
