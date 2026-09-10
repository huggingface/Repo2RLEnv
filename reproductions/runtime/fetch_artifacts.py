"""Download the recorded task/evidence archive and verify it before extraction.

This transfers task definitions and logs, never Docker or Apptainer images.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tarfile
from pathlib import Path

from dotenv import dotenv_values
from huggingface_hub import hf_hub_download


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.destination.exists():
        raise FileExistsError(args.destination)
    record = json.loads(args.receipt.read_text())
    token = os.environ.get("HF_TOKEN") or dotenv_values(
        Path(__file__).resolve().parents[2] / ".env"
    ).get("HF_TOKEN")
    archive = Path(
        hf_hub_download(
            repo_id=record["repo_id"],
            repo_type="dataset",
            revision=record["revision"],
            filename=record["archive_path"],
            token=token,
        )
    )
    if hashlib.sha256(archive.read_bytes()).hexdigest() != record["archive_sha256"]:
        raise ValueError("Archive checksum differs from the recorded reproduction")
    with tarfile.open(archive) as stream:
        if any(not (entry.isfile() or entry.isdir()) for entry in stream.getmembers()):
            raise ValueError("Artifact archive contains a link or special file")
        stream.extractall(args.destination, filter="data")
    print(json.dumps({"extracted": str(args.destination), "revision": record["revision"]}))


if __name__ == "__main__":
    main()
