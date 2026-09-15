"""Resolve explicit public PR inputs before remote execution."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from urllib.parse import urlparse

from repo2rlenv.github import _run_gh, fetch_issue, fetch_pr_diff
from repo2rlenv.pipelines.pr_runtime import _linked_issue_number


def fetch_source(url: str, options) -> dict:
    parsed = urlparse(url)
    if parsed.netloc != "github.com":
        raise ValueError("SWE-gen currently supports public GitHub PRs")
    owner, name, _, number = parsed.path.strip("/").split("/")
    endpoint = f"repos/{owner}/{name}/pulls/{number}"
    pull = json.loads(_run_gh(["api", endpoint]))
    if not pull.get("merged_at") or pull["base"]["repo"]["private"]:
        raise ValueError("This profile requires a merged public PR")
    rows = json.loads(_run_gh(["api", endpoint + "/files?per_page=100", "--paginate", "--slurp"]))
    changed = [item for page in rows for item in page]
    roots = [PurePosixPath(path) for path in options.source_paths]
    tests = [PurePosixPath(path) for path in options.test_paths]

    def within(path, prefixes):
        return any(path == root or root in path.parents for root in prefixes)

    source_files = []
    for item in changed:
        path = PurePosixPath(item["filename"])
        if within(path, roots) and not within(path, tests):
            if item["status"] != "modified" or path.suffix != ".py":
                raise ValueError(
                    "Current Python artifact profile requires modified existing .py files"
                )
            source_files.append(str(path))
    if not source_files:
        raise ValueError("PR has no source changes in the supplied profile")
    diff = fetch_pr_diff(owner, name, int(number))
    blocks = re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE)
    selected = []
    for block in blocks:
        header = re.match(r"diff --git a/(.+) b/(.+)\n", block)
        if header and header.group(1) == header.group(2) and header.group(1) in source_files:
            selected.append(block)
    if len(selected) != len(source_files):
        raise ValueError("Cannot map the complete source diff to the supported file profile")
    linked = _linked_issue_number(pull.get("body") or "")
    issue = fetch_issue(owner, name, linked) if linked else None
    identity = {"url": url, "head": pull["head"]["sha"], "source_diff": "".join(selected)}
    return {
        **identity,
        "id": hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20],
        "repo": f"https://github.com/{owner}/{name}",
        "base": pull["base"]["sha"],
        "title": pull["title"],
        "body": (pull.get("body") or "")[:10000],
        "linked_issue": {"title": issue[0], "body": issue[1][:10000]} if issue else None,
        "source_files": source_files,
        "test_files": [
            item["filename"] for item in changed if within(PurePosixPath(item["filename"]), tests)
        ],
    }
