"""Original downloader on three upstream example IDs; text transcripts only."""

import hashlib
import json
import logging
from pathlib import Path
from urllib.robotparser import RobotFileParser

import requests
from data_filtering.detect_pii import classify_recording
from data_retrieval.config import CrawlerConfig
from data_retrieval.download_recordings import RecordingDownloader

logging.basicConfig(level=logging.INFO)
root = Path("/work/terminalworld/recordings")
config = CrawlerConfig(output_dir=root, request_timeout=30, retry_count=1)
downloader = RecordingDownloader(config, max_workers=1)
response = requests.get("https://asciinema.org/robots.txt", timeout=30)
response.raise_for_status()
Path("/evidence/terminalworld/robots.txt").write_text(response.text)
robots = RobotFileParser()
robots.parse(response.text.splitlines())
results = []
for rec_id in ["100135", "1060", "188971"]:
    url = f"https://asciinema.org/a/{rec_id}"
    permitted = all(robots.can_fetch(config.user_agent, u) for u in [url, url + ".txt"])
    receipt = {"id": rec_id, "url": url, "robots_allowed": permitted}
    if permitted:
        receipt["native_return"] = downloader.download_single({"href": f"/a/{rec_id}"})
        transcript = root / rec_id / "recording.txt"
        receipt["transcript_downloaded"] = transcript.is_file() and transcript.stat().st_size > 0
        if receipt["transcript_downloaded"]:
            receipt["transcript_sha256"] = hashlib.sha256(transcript.read_bytes()).hexdigest()
            _, receipt["native_pii_filter"] = classify_recording(root / rec_id)
    results.append(receipt)
    Path("/evidence/terminalworld/retrieval.json").write_text(json.dumps(results, indent=2))
# A downloaded transcript is input acquisition, not a generated environment.
print(json.dumps(results, indent=2))
