"""Resolve merged PR metadata on the controller without exposing credentials remotely."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.github import _run_gh


def merged_pulls(url: str, options, receipt: Path) -> list[dict]:
    if receipt.exists():
        return json.loads(receipt.read_text())
    path = urlparse(url).path.strip("/").removesuffix(".git")
    rows = []
    if options.pr_numbers:
        rows = [
            json.loads(_run_gh(["api", f"repos/{path}/pulls/{number}"]))
            for number in options.pr_numbers
        ]
    else:
        for page in range(1, (options.max_prs + 99) // 100 + 1):
            batch = json.loads(
                _run_gh(
                    [
                        "api",
                        f"repos/{path}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page={page}",
                    ]
                )
            )
            rows.extend(batch)
            if len(batch) < 100:
                break
        rows = rows[: options.max_prs]
    selected = [
        {
            "number": row["number"],
            "head": row["merge_commit_sha"],
            "title": row["title"],
            "body": (row.get("body") or "")[:10000],
            "url": row["html_url"],
            "merged_at": row["merged_at"],
        }
        for row in rows
        if row.get("merged_at") and row.get("merge_commit_sha")
    ]
    save_record(receipt, selected)
    return selected
