"""Build healthy PR heads, reverse source changes, and measure test contrast."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.execution.python_repository import bootstrap_snapshot, test_image
from repo2rlenv.quality.python_evidence import test_excerpts
from repo2rlenv.quality.test_results import execution_contrast
from repo2rlenv.spec.input import RepoSpec
from repo2rlenv.spec.recipe_options import PRRecipeOptions
from repo2rlenv.ui import console


def generate(source: dict, options: PRRecipeOptions, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=False)
    repo = RepoSpec(url=source["repo"], ref=source["head"], access="public")
    boot, base = bootstrap_snapshot(repo, options, destination)
    healthy = test_image(boot.image_digest, options, destination / "healthy")
    if healthy.returncode or not healthy.passed:
        raise ValueError("Healthy PR head does not pass the configured test suite")
    defective = destination / "defective-source"
    defective.mkdir()
    for relative in source["source_files"]:
        path = defective / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(base / relative, path)
    patch = destination / "source.diff"
    patch.write_text(source["source_diff"])
    result = subprocess.run(
        ["git", "apply", "--reverse", str(patch.resolve())],
        cwd=defective,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    (destination / "reverse-patch.stderr").write_text(result.stderr)
    if result.returncode:
        raise ValueError("PR source changes cannot be reversed on the pinned healthy head")
    broken = test_image(
        boot.image_digest,
        options,
        destination / "defective",
        replacements={relative: defective / relative for relative in source["source_files"]},
    )
    contrast = execution_contrast(healthy, broken)
    excerpts = test_excerpts(base, contrast["FAIL_TO_PASS"])
    record = {
        **source,
        "contrast": contrast,
        "image_digest": boot.image_digest,
        "test_evidence": excerpts,
    }
    save_record(destination / "candidate.json", record)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    data = json.loads(args.config.read_text())
    result = generate(data["source"], PRRecipeOptions.model_validate(data["options"]), args.output)
    console.json({"id": result["id"], "contrast": result["contrast"]})


if __name__ == "__main__":
    main()
