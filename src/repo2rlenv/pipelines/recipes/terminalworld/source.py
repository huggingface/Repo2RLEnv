"""Acquire public text recordings and load the native recording-directory layout."""

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

from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.pipelines.recipes.terminalworld.privacy import screen
from repo2rlenv.ui import console

_USER_AGENT = "Repo2RLEnv-RecordingResearch/1.0"
_LIMIT = 160000


class MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.metadata = {}
        self.description = []
        self._description_depth = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("property") in ("og:title", "og:description"):
            self.metadata[attributes["property"].removeprefix("og:")] = attributes.get(
                "content", ""
            )
        if tag == "div":
            if self._description_depth:
                self._description_depth += 1
            elif "description" in attributes.get("class", "").split():
                self._description_depth = 1

    def handle_endtag(self, tag):
        if tag == "div" and self._description_depth:
            self._description_depth -= 1

    def handle_data(self, data):
        if self._description_depth:
            self.description.append(data)


def _download(client, url: str, limit: int) -> str:
    with client.stream("GET", url) as response:
        response.raise_for_status()
        chunks = []
        length = 0
        for chunk in response.iter_bytes():
            length += len(chunk)
            if length > limit:
                raise ValueError("Recording response exceeds the configured text limit")
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def fetch_recordings(ids: list[str], destination: Path) -> dict:
    """Fetch metadata and .txt only; retain per-input outcomes for restart."""
    if (
        not ids
        or len(ids) > 2000
        or any(re.fullmatch(r"[0-9]{1,12}", value) is None for value in ids)
    ):
        raise ValueError("Supply one to 2000 numeric public recording IDs")
    destination.mkdir(parents=True, exist_ok=True)
    receipt = destination / "retrieval.json"
    results = json.loads(receipt.read_text()) if receipt.exists() else {}
    with httpx.Client(
        timeout=30, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
    ) as client:
        rules = _download(client, "https://asciinema.org/robots.txt", 64000)
        (destination / "robots.txt").write_text(rules)
        robots = RobotFileParser()
        robots.parse(rules.splitlines())
        for rec_id in dict.fromkeys(ids):
            folder = destination / rec_id
            if rec_id in results:
                if results[rec_id].get("downloaded") and not (folder / "recording.txt").is_file():
                    raise ValueError("Retrieved recording disappeared; restore its recorded input")
                continue
            url = f"https://asciinema.org/a/{rec_id}"
            result = {"url": url, "downloaded": False}
            try:
                if not all(robots.can_fetch(_USER_AGENT, path) for path in (url, url + ".txt")):
                    raise ValueError("Recording page or text disallowed by robots.txt")
                metadata = MetadataParser()
                metadata.feed(_download(client, url, 1024 * 1024))
                text = _download(client, url + ".txt", _LIMIT)
                if len(text.strip()) < 100 or text.lstrip().lower().startswith("<!doctype html"):
                    raise ValueError("No usable plain-text recording")
                folder.mkdir(exist_ok=True)
                (folder / "recording.txt").write_text(text)
                save_record(
                    folder / "info.json",
                    {
                        **metadata.metadata,
                        "id": rec_id,
                        "url": url,
                        "description": " ".join(metadata.description).strip()
                        or metadata.metadata.get("description", ""),
                        "transcript_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    },
                )
                result.update(
                    downloaded=True, transcript_sha256=hashlib.sha256(text.encode()).hexdigest()
                )
            except (httpx.HTTPError, ValueError) as exc:
                result["error_type"] = type(exc).__name__
                if isinstance(exc, httpx.HTTPStatusError):
                    result["http_status"] = exc.response.status_code
            results[rec_id] = result
            save_record(receipt, results)
            time.sleep(1)
    return results


def load_recordings(path: Path) -> list[dict]:
    folders = (
        [path]
        if (path / "recording.txt").is_file()
        else sorted(p for p in path.iterdir() if p.is_dir())
    )
    records = []
    for folder in folders:
        transcript = folder / "recording.txt"
        info = folder / "info.json"
        if not transcript.is_file() or not info.is_file():
            continue
        if transcript.stat().st_size > _LIMIT or info.stat().st_size > 64000:
            raise ValueError("Recording exceeds the supported bounded input profile")
        text = transcript.read_text()
        metadata = json.loads(info.read_text())
        flags = screen(text)
        records.append(
            {
                "source": "asciinema_public_recording",
                "id": str(metadata.get("id", folder.name)),
                "title": metadata.get("title") or folder.name,
                "url": metadata.get("url") or f"https://asciinema.org/a/{folder.name}",
                "description": metadata.get("description", ""),
                "transcript": "" if flags else text,
                "transcript_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "filter_flags": flags,
            }
        )
    if not records:
        raise ValueError("No native info.json/recording.txt input pairs found")
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids-json", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = fetch_recordings(json.loads(args.ids_json.read_text()), args.out)
    console.kv(
        {
            "attempted": len(result),
            "downloaded": sum(bool(r["downloaded"]) for r in result.values()),
        },
        title="Recording acquisition",
    )


if __name__ == "__main__":
    main()
