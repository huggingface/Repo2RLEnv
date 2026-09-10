"""Upload text reproduction recipes, excluding credentials and run data."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import tempfile
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
    sources = []
    for experiment in dict.fromkeys(args.experiments):
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
            if source.suffix not in {
                ".py",
                ".sh",
                ".patch",
                ".yaml",
                ".toml",
                ".in",
            } and source.name not in {"requirements.lock.txt", "requirements.txt"}:
                continue
            sources.append((source, relative))
    # One upload avoids a provider round trip per small source file. This is
    # source text only; container images and generated execution remain remote.
    with tempfile.TemporaryDirectory(prefix="reproduction-recipes-") as temporary:
        archive = Path(temporary) / "recipes.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for source, relative in sources:
                bundle.add(source, arcname=str(relative), recursive=False)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        remote = f"/work/recipe-bundles/{digest}.tar.gz"
        sandbox.filesystem.make_directory("/work/recipe-bundles")
        sandbox.filesystem.copy_from_local(str(archive), remote)
        program = (
            "import hashlib,pathlib,tarfile,sys; p=pathlib.Path(sys.argv[1]); "
            "assert hashlib.sha256(p.read_bytes()).hexdigest()==sys.argv[2]; "
            "tarfile.open(p).extractall('/work/recipes',filter='data')"
        )
        process = sandbox.exec("python", "-c", program, remote, digest, timeout=60)
        process.wait()
        if process.returncode:
            raise RuntimeError(f"Remote recipe extraction failed: {process.stderr.read()}")
    print(f"Uploaded {len(sources)} recipe files; archive sha256={digest}")


if __name__ == "__main__":
    main()
