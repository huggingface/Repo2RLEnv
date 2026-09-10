"""Upload only committed recipe types, excluding credentials and run data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("experiments", nargs="+")
    args = parser.parse_args()
    receipt = json.loads((ROOT / "runs" / f"{args.name}.json").read_text())
    sandbox = modal.Sandbox.from_id(receipt["sandbox_id"])
    count = 0
    for experiment in args.experiments:
        directory = (ROOT / experiment).resolve()
        if directory.parent != ROOT or not directory.is_dir():
            raise ValueError("Expected immediate reproduction folder")
        for source in directory.rglob("*"):
            relative = source.relative_to(ROOT)
            if any(
                part in {"upstream", "runs", "harbor", "data", "cache", ".venv"}
                for part in relative.parts
            ):
                continue
            if source.is_symlink() or not source.is_file():
                continue
            if (
                source.suffix not in {".py", ".sh", ".patch", ".yaml", ".toml", ".in"}
                and source.name != "requirements.lock.txt"
            ):
                continue
            destination = Path("/work/recipes") / relative
            sandbox.filesystem.make_directory(str(destination.parent))
            sandbox.filesystem.copy_from_local(str(source), str(destination))
            count += 1
    print(f"Uploaded {count} recipe files")


if __name__ == "__main__":
    main()
