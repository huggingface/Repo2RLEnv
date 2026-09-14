"""Check release archives and a base-only wheel install without optional SDKs.

Run with the freshly installed wheel's Python: tests/check_distribution.py DIST.
No remote execution or model calls are made.
"""

from __future__ import annotations

import argparse
import email
import json
import os
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path


def check(dist: Path) -> dict:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    wheels, sources = list(dist.glob("*.whl")), list(dist.glob("*.tar.gz"))
    assert len(wheels) == len(sources) == 1, "Use a clean distribution directory"
    with zipfile.ZipFile(wheels[0]) as wheel, tarfile.open(sources[0]) as sdist:
        metadata_path = next(n for n in wheel.namelist() if n.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(wheel.read(metadata_path))
        assert metadata["License-Expression"] == project["license"]
        license_paths = {
            path.relative_to(root).as_posix()
            for pattern in project["license-files"]
            for path in root.glob(pattern)
        }
        assert set(metadata.get_all("License-File", [])) == license_paths
        dist_info = metadata_path.rsplit("/", 1)[0]
        prefix = f"{project['name']}-{project['version']}"
        for name in license_paths:
            expected = (root / name).read_bytes()
            assert wheel.read(f"{dist_info}/licenses/{name}") == expected
            assert sdist.extractfile(f"{prefix}/{name}").read() == expected
        # Verify every tracked package resource, including prompts and runtime JS.
        # Caches from local test runs are not package inputs.
        count = 0
        for path in (root / "src/repo2rlenv").rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            name = path.relative_to(root / "src").as_posix()
            assert wheel.read(name) == path.read_bytes(), name
            assert sdist.extractfile(f"{prefix}/src/{name}").read() == path.read_bytes(), name
            count += 1
        for name in wheel.namelist():
            assert not ({"workspace", "node_modules", ".env", "references"} & set(Path(name).parts))
        pkg_info = email.message_from_bytes(sdist.extractfile(f"{prefix}/PKG-INFO").read())
        assert pkg_info["License-Expression"] == metadata["License-Expression"]
        assert set(pkg_info.get_all("License-File", [])) == license_paths

    def cli(*args: str, expected: int = 0):
        result = subprocess.run(
            [sys.executable, "-m", "repo2rlenv.cli", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            cwd=dist.resolve(),
            env={**os.environ, "PYTHON_DOTENV_DISABLED": "1"},
        )
        assert result.returncode == expected, result.stderr + result.stdout
        return json.loads(result.stdout)

    listing = cli("pipelines", "list", "--json")
    implemented = [item for item in listing["recipes"] if item["implemented"]]
    for item in implemented:
        detail = cli("pipelines", "describe", item["pipeline"], "--recipe", item["id"], "--json")
        assert detail["id"] == item["id"]
    for args in (
        ["generate", "--config", "missing.yaml"],
        ["tasksmith", "show", "missing.json"],
        ["quality", "show", "missing.json"],
    ):
        assert set(cli(*args, "--json", expected=2)) == {"error", "message"}
    return {
        "package_files": count,
        "license_files": len(license_paths),
        "recipe_discovery": len(implemented),
        "status": "passed",
    }


if __name__ == "__main__":
    from repo2rlenv.ui import console

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path)
    console.json(check(parser.parse_args().dist))
