"""Freeze public PR evidence before any paid operation."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath

from repo2rlenv.github import _run_gh, fetch_pr_diff


def validate_source_records(records: list[dict], urls: list[str]) -> list[dict]:
    """Check frozen intake identities without fetching or executing target code."""
    if [row.get("url") for row in records] != urls:
        raise ValueError("Frozen PR records must match panel URLs in order")
    for row in records:
        url = row["url"]
        match = re.fullmatch(r"https://github.com/([\w.-]+)/([\w.-]+)/pull/([1-9][0-9]*)", url)
        if not match or row.get("repo") != f"https://github.com/{match[1]}/{match[2]}":
            raise ValueError("Frozen PR repository does not match its URL")
        if any(
            not re.fullmatch(r"[0-9a-f]{40}", str(row.get(key, ""))) for key in ("base", "head")
        ):
            raise ValueError("Frozen PR records require immutable base and head commits")
        identity = {key: row.get(key) for key in ("url", "head", "source_diff")}
        if not isinstance(identity["source_diff"], str) or not identity["source_diff"]:
            raise ValueError("Frozen PR source diff is missing")
        expected = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
        if row.get("id") != expected:
            raise ValueError("Frozen PR source identity changed")
        paths = row.get("source_files", [])
        operations = row.get("source_operations", [])
        if not paths or len(set(paths)) != len(paths) or len(operations) != len(paths):
            raise ValueError("Frozen PR source inventory is incomplete")
        for path in paths:
            pure = PurePosixPath(path)
            if pure.is_absolute() or ".." in pure.parts or pure.suffix != ".py":
                raise ValueError("Frozen PR source paths must be relative Python files")
        if {entry.get("path") for entry in operations} != set(paths) or any(
            entry.get("operation") not in {"added", "modified"} for entry in operations
        ):
            raise ValueError("Frozen PR source operations are unsupported or incomplete")
        headers = re.findall(r"^diff --git a/(.+) b/(.+)$", row["source_diff"], re.MULTILINE)
        if len(headers) != len(paths) or {
            before for before, after in headers if before == after
        } != set(paths):
            raise ValueError("Frozen PR diff does not cover its source inventory")
        if row.get("workspace_strategy") != "head_minus_source_patch":
            raise ValueError("Frozen PR workspace strategy is unsupported")
    return records


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
            if row["status"] not in {"modified", "added"}:
                raise ValueError(f"Unsupported renamed/deleted source path: {path}")
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
        raise ValueError("PR has no added or modified Python source files")
    diff = fetch_pr_diff(owner, name, int(number))
    blocks = []
    for block in re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE):
        header = re.match(r"diff --git a/(.+) b/(.+)\n", block)
        if header and header.group(1) == header.group(2) and header.group(1) in source_files:
            blocks.append(block)
    if len(blocks) != len(source_files):
        raise ValueError("Source diff is incomplete or has unsupported filenames")
    if len(changed) != pull.get("changed_files", len(changed)):
        raise ValueError("PR file inventory is incomplete")
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
        "created_at": pull.get("created_at"),
        "merged_at": pull["merged_at"],
        "body": (pull.get("body") or "")[:10000],
        "source_files": source_files,
        "source_operations": [
            {"path": row["filename"], "operation": row["status"]}
            for row in changed
            if row["filename"] in source_files
        ],
        "changed_files": [
            {key: row[key] for key in ("filename", "status", "additions", "deletions")}
            for row in changed
        ],
        "full_diff": diff,
        "workspace_strategy": "head_minus_source_patch",
    }
