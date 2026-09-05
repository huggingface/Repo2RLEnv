from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

PR_PATTERN = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)")


def api(path: str):
    result = subprocess.run(
        ["gh", "api", path], check=True, capture_output=True, text=True, timeout=90
    )
    return json.loads(result.stdout)


def parse_seeds(path: Path) -> list[str]:
    return list(dict.fromkeys(m.group(0) for m in PR_PATTERN.finditer(path.read_text())))


def resolve_pr(url: str) -> dict:
    match = PR_PATTERN.fullmatch(url.rstrip("/"))
    if not match:
        raise ValueError("Expected a GitHub pull request URL")
    repo, number = match.groups()
    data = api(f"repos/{repo}/pulls/{number}")
    if not data["merged"]:
        raise ValueError("Only merged PRs can supply a verified reference change")
    if data["base"]["repo"].get("private"):
        raise ValueError("This curation profile currently accepts public repositories only")
    base, head = data["base"]["sha"], data["head"]["sha"]
    comparison = api(f"repos/{repo}/compare/{base}...{head}")
    base = comparison["merge_base_commit"]["sha"]
    return {
        "id": repo.replace("/", "-").replace("_", "-").lower() + "-" + number,
        "repo": repo,
        "url": url,
        "number": int(number),
        "base_sha": base,
        "head_sha": head,
        "title": data["title"],
        "body": data["body"] or "",
        "merged_at": data["merged_at"],
        "changed_files": data["changed_files"],
        "additions": data["additions"],
        "deletions": data["deletions"],
    }


def repo_seeds(repo: str, limit: int = 60) -> list[str]:
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("Repository must be owner/name")
    data = api(
        f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page={min(limit, 100)}"
    )
    return [p["html_url"] for p in data if p.get("merged_at")]
