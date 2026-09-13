"""Freeze public PR evidence before any paid operation."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath

from repo2rlenv.github import _run_gh, fetch_pr_diff


def resolve_pr(url: str) -> dict:
    match = re.fullmatch(r"https://github.com/([\w.-]+)/([\w.-]+)/pull/([1-9][0-9]*)/?", url)
    if not match:
        raise ValueError("Use a public GitHub PR URL")
    owner, name, number = match.groups()
    endpoint = f"repos/{owner}/{name}/pulls/{number}"
    pull = json.loads(_run_gh(["api", endpoint]))
    if not pull.get("merged_at") or pull["base"]["repo"]["private"]:
        raise ValueError("Tasksmith's first profile requires merged public PRs")
    pages = json.loads(_run_gh(["api", endpoint + "/files?per_page=100", "--paginate", "--slurp"]))
    changed = [row for page in pages for row in page]
    source_files = []
    for row in changed:
        path = PurePosixPath(row["filename"])
        is_test = (
            any(part in {"test", "tests", "testing"} for part in path.parts)
            or path.name == "conftest.py"
            or path.name.startswith("test_")
        )
        if path.suffix == ".py" and not is_test:
            if row["status"] != "modified":
                raise ValueError(f"Unsupported added/deleted source path: {path}")
            source_files.append(str(path))
        elif not is_test and path.suffix in {
            ".c",
            ".cc",
            ".cpp",
            ".h",
            ".rs",
            ".go",
            ".js",
            ".ts",
            ".cu",
        }:
            raise ValueError(f"PR includes non-Python source changes: {path}")
    if not source_files:
        raise ValueError("PR has no modified Python source files")
    diff = fetch_pr_diff(owner, name, int(number))
    blocks = []
    for block in re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE):
        header = re.match(r"diff --git a/(.+) b/(.+)\n", block)
        if header and header.group(1) == header.group(2) and header.group(1) in source_files:
            blocks.append(block)
    if len(blocks) != len(source_files):
        raise ValueError("Source diff is incomplete or has unsupported filenames")
    # Re-read identity after retrieving paginated files and diff to detect force pushes.
    current = json.loads(_run_gh(["api", endpoint]))
    if (current["head"]["sha"], current["base"]["sha"]) != (
        pull["head"]["sha"],
        pull["base"]["sha"],
    ):
        raise ValueError("PR changed during intake; freeze it again before spending")
    identity = {"url": url.rstrip("/"), "head": pull["head"]["sha"], "source_diff": "".join(blocks)}
    return {
        **identity,
        "id": hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12],
        "repo": f"https://github.com/{owner}/{name}",
        "base": pull["base"]["sha"],
        "title": pull["title"],
        "body": (pull.get("body") or "")[:10000],
        "source_files": source_files,
        "changed_files": [
            {key: row[key] for key in ("filename", "status", "additions", "deletions")}
            for row in changed
        ],
        "full_diff": diff,
        "workspace_strategy": "head_minus_source_patch",
    }
