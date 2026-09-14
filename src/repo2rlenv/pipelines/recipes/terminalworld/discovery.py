"""Bounded public recording discovery, following TerminalWorld's explore feeds.

Only numeric source IDs are collected. Each page's identity and outcome is
retained, so a restart reuses completed reads rather than changing the input pool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.robotparser import RobotFileParser

import httpx

from repo2rlenv.execution.lifecycle import now, save_record
from repo2rlenv.pipelines.recipes.terminalworld.source import _USER_AGENT, _download
from repo2rlenv.ui import console

FEEDS = {
    "public": "/explore/public",
    "recent": "/explore/recordings/recent",
    "featured": "/explore/recordings/featured",
    "popular": "/explore/recordings/popular",
}
_ORIGIN = "https://asciinema.org"


class RecordingLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        match = re.fullmatch(r"(?:https://asciinema\.org)?/a/([0-9]{1,12})/?", href)
        if match and match[1] not in self.ids:
            self.ids.append(match[1])


def discover_recordings(
    destination: Path, *, feeds: list[str], pages_per_feed: int = 5
) -> list[str]:
    """Cache bounded, robots-permitted index pages; never fetch task solutions."""
    if not feeds or any(feed not in FEEDS for feed in feeds):
        raise ValueError("Choose one or more supported public recording feeds")
    if not 1 <= pages_per_feed <= 50:
        raise ValueError("Discovery requires one to 50 pages per feed")
    destination.mkdir(parents=True, exist_ok=True)
    selected: list[str] = []
    with httpx.Client(
        timeout=30, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
    ) as client:
        rules = _download(client, _ORIGIN + "/robots.txt", 64000)
        (destination / "robots.txt").write_text(rules)
        robots = RobotFileParser()
        robots.parse(rules.splitlines())
        for feed in dict.fromkeys(feeds):
            prior_pages: set[str] = set()
            for page in range(1, pages_per_feed + 1):
                url = _ORIGIN + FEEDS[feed] + f"?page={page}"
                receipt = destination / "pages" / f"{feed}-{page:03d}.json"
                if receipt.exists():
                    record = json.loads(receipt.read_text())
                    if record.get("url") != url:
                        raise ValueError("Cached discovery page belongs to a different source")
                else:
                    record = {"url": url, "checked_at": now(), "ids": []}
                    try:
                        if not robots.can_fetch(_USER_AGENT, url):
                            raise ValueError("Recording index disallowed by robots.txt")
                        html = _download(client, url, 1024 * 1024)
                        parser = RecordingLinks()
                        parser.feed(html)
                        record.update(
                            ids=parser.ids,
                            html_sha256=hashlib.sha256(html.encode()).hexdigest(),
                        )
                    except (httpx.HTTPError, ValueError) as exc:
                        record["error_type"] = type(exc).__name__
                        if isinstance(exc, httpx.HTTPStatusError):
                            record["http_status"] = exc.response.status_code
                    save_record(receipt, record)
                    time.sleep(1)
                if any(
                    not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,12}", value)
                    for value in record["ids"]
                ):
                    raise ValueError("Cached recording IDs are invalid")
                selected.extend(value for value in record["ids"] if value not in selected)
                signature = hashlib.sha256(json.dumps(record["ids"]).encode()).hexdigest()
                # Empty, failed or repeated pages end this feed. Other feeds can
                # still supply IDs without retrying a failed page in this run.
                if not record["ids"] or signature in prior_pages:
                    break
                prior_pages.add(signature)
    save_record(destination / "ids.json", selected)
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--feeds", nargs="+", choices=sorted(FEEDS), default=list(FEEDS))
    parser.add_argument("--pages-per-feed", type=int, default=5)
    args = parser.parse_args()
    ids = discover_recordings(args.out, feeds=args.feeds, pages_per_feed=args.pages_per_feed)
    console.kv({"recording_ids": len(ids), "path": args.out / "ids.json"}, title="Discovery")


if __name__ == "__main__":
    main()
