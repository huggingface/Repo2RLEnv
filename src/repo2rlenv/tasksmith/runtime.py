"""Install pinned essential agent libraries outside source and wheel trees."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path


def sources() -> Path:
    return Path(__file__).parent / "author/runtimes"


def directory() -> Path:
    key = hashlib.sha256(
        b"".join(path.read_bytes() for path in sorted(sources().iterdir()) if path.is_file())
    ).hexdigest()[:20]
    return Path.home() / ".cache/repo2rlenv/tasksmith/runtimes" / key


def install() -> Path:
    target = directory()
    target.mkdir(parents=True, exist_ok=True)
    for source in sources().iterdir():
        if source.is_file():
            shutil.copyfile(source, target / source.name)
    subprocess.run(
        ["npm", "ci", "--prefix", str(target), "--no-audit", "--no-fund"], check=True, timeout=600
    )
    return target
