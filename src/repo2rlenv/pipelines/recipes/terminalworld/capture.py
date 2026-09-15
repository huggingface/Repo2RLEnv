"""Runs inside a disposable container to capture a reference's filesystem changes."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def filesystem_state(roots: list[Path], limit: int = 4000) -> dict:
    result = {}
    for root in roots:
        if not root.is_dir():
            continue
        for directory, children, names in os.walk(root, followlinks=False):
            children[:] = sorted(
                name
                for name in children
                if name not in (".git", "__pycache__", ".venv", "node_modules")
            )
            for name in sorted(names):
                path = Path(directory) / name
                if path.is_symlink() or not path.is_file():
                    continue
                if len(result) >= limit:
                    return result
                try:
                    stat = path.stat()
                    with path.open("rb") as handle:
                        prefix = handle.read(65536)
                    result[str(path)] = {
                        "size": stat.st_size,
                        "mode": stat.st_mode & 0o777,
                        "mtime_ns": stat.st_mtime_ns,
                        "prefix_sha256": hashlib.sha256(prefix).hexdigest(),
                    }
                except OSError:
                    continue
    return result


def changes(before: dict, after: dict) -> dict:
    return {
        "created": [path for path in after if path not in before],
        "modified": [path for path in after if path in before and after[path] != before[path]],
        "deleted": [path for path in before if path not in after],
    }


def main() -> None:
    roots = [Path(path) for path in ("/workspace", "/app", "/home/user")]
    before = filesystem_state(roots)
    output = Path("/tmp/reference-output.txt")
    timed_out = False
    with output.open("w") as handle:
        try:
            result = subprocess.run(
                ["bash", sys.argv[1]],
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=int(sys.argv[2]),
            )
            returncode = result.returncode
        except subprocess.TimeoutExpired:
            timed_out, returncode = True, 124
    after = filesystem_state(roots)
    delta = changes(before, after)
    contents = {}
    for path in (delta["created"] + delta["modified"])[:40]:
        try:
            with Path(path).open("rb") as handle:
                prefix = handle.read(2048)
            contents[path] = prefix.decode("utf-8") if b"\0" not in prefix else "<binary file>"
        except (OSError, UnicodeDecodeError):
            contents[path] = "<unreadable or binary>"
    with output.open() as handle:
        stdout = handle.read(16000)
    Path("/tmp/recording-snapshot.json").write_text(
        json.dumps(
            {
                "returncode": returncode,
                "timed_out": timed_out,
                "solution_stdout": stdout,
                "changes": delta,
                "changed_contents": contents,
                "initial_files": before,
                "final_files": after,
                "file_scan_limit": 4000,
                "content_prefix_limit": 2048,
            }
        )
    )


if __name__ == "__main__":
    main()
