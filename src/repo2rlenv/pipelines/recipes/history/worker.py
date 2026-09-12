"""Remote history selection and execution of identical post-change tests."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path

from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.execution.python_repository import bootstrap_snapshot, test_image
from repo2rlenv.pipelines.recipes.history.selection import check_entities, within
from repo2rlenv.quality.python_evidence import test_excerpts
from repo2rlenv.quality.test_results import execution_contrast
from repo2rlenv.spec.input import RepoSpec
from repo2rlenv.spec.recipe_options import (
    HistoryRecipeOptions,
    PythonRepositoryProfile,
    R2EGymOptions,
    SWENextOptions,
)
from repo2rlenv.ui import console


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=120, check=True
    ).stdout


def snapshot(root: Path, revision: str, destination: Path) -> None:
    archive = subprocess.run(
        ["git", "-C", str(root), "archive", revision], capture_output=True, timeout=120, check=True
    ).stdout
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for item in tar:
            path = Path(item.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or item.issym()
                or item.islnk()
                or not (item.isfile() or item.isdir())
            ):
                raise ValueError("Unsupported path in historical source snapshot")
            if any(part in {".git", "__pycache__"} for part in path.parts):
                raise ValueError("Historical snapshot contains cache or Git internals")
            target = destination / path
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(item).read())
                target.chmod(0o755 if item.mode & 0o111 else 0o644)


def candidate_for(
    root: Path, head: str, repo: RepoSpec, options: HistoryRecipeOptions, context: dict
) -> dict:
    resolved = git(root, "rev-parse", "--verify", head + "^{commit}").strip()
    parent = git(root, "rev-parse", resolved + "^1").strip()
    rows = [
        line.split("\t")
        for line in git(
            root, "diff", "--name-status", "--no-renames", parent, resolved
        ).splitlines()
    ]
    source_pairs, test_pairs = {}, {}
    for status, path in rows:
        if not path.endswith(".py"):
            continue
        test = within(path, options.test_paths)
        if not test and not within(path, options.source_paths):
            raise ValueError("Python changes outside the configured source/test profile")
        if status not in ({"M", "A"} if test else {"M"}):
            raise ValueError("Profile supports existing source files and added/edited test files")
        old = git(root, "show", f"{parent}:{path}") if status != "A" else ""
        new = git(root, "show", f"{resolved}:{path}")
        (test_pairs if test else source_pairs)[path] = (old, new)
    if not source_pairs or not test_pairs or len(source_pairs) >= options.max_non_test_files:
        raise ValueError("No bounded source-plus-test change")
    source_diff = git(root, "diff", parent, resolved, "--", *source_pairs)
    edited_lines = sum(
        int(row.split()[0]) + int(row.split()[1])
        for row in git(
            root, "diff", "--numstat", parent, resolved, "--", *source_pairs
        ).splitlines()
    )
    patch = git(root, "diff", parent, resolved)
    if edited_lines >= options.max_non_test_edited_lines or len(patch) >= options.max_patch_length:
        raise ValueError("Change exceeds native size bounds")
    selection = check_entities(source_pairs, test_pairs, options)
    tests = sorted({row["file"] for row in selection["test_entities"]})
    return {
        "id": hashlib.sha256(f"{repo.url}:{parent}:{resolved}".encode()).hexdigest()[:20],
        "repo": repo.url,
        "base": parent,
        "head": resolved,
        "title": context.get("title") or git(root, "show", "-s", "--format=%s", resolved).strip(),
        "message": git(root, "show", "-s", "--format=%B", resolved)[:10000],
        "context": context,
        "source_files": sorted(source_pairs),
        "test_files": tests,
        "source_diff": source_diff,
        "test_diff": git(root, "diff", parent, resolved, "--", *tests),
        "selection": selection,
    }


def prepare(config: dict, destination: Path) -> dict:
    repo = RepoSpec.model_validate(config["repo"])
    recipe = config["recipe"]
    options = (SWENextOptions if recipe == "swe_next" else R2EGymOptions).model_validate(
        config["options"]
    )
    destination.mkdir(parents=True, exist_ok=False)
    root = Path("/work/history") / hashlib.sha256(repo.url.encode()).hexdigest()[:16]
    root.parent.mkdir(parents=True, exist_ok=True)
    with root.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not root.exists():
            temporary = root.with_name(root.name + "-" + uuid.uuid4().hex)
            try:
                subprocess.run(
                    ["git", "clone", "--no-checkout", repo.url, str(temporary)],
                    capture_output=True,
                    timeout=300,
                    check=True,
                )
                temporary.rename(root)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
    revision = git(root, "rev-parse", repo.ref or "HEAD").strip()
    if recipe == "swe_next":
        items = config["pulls"]
    else:
        items = [
            {"head": sha}
            for sha in git(
                root, "rev-list", "--first-parent", f"--max-count={options.history_limit}", revision
            ).splitlines()
        ]
    candidates, rejected = [], []
    for item in items:
        if len(candidates) >= options.max_candidates:
            break
        try:
            value = candidate_for(root, item["head"], repo, options, item)
            if any(row["head"] == value["head"] for row in candidates):
                continue
            candidates.append(value)
        except (ValueError, SyntaxError, subprocess.CalledProcessError) as exc:
            rejected.append(
                {"head": item["head"], "reason": "source_filter", "detail": str(exc)[:500]}
            )
    result = {
        "candidates": candidates,
        "rejected": rejected,
        "attempted": len(candidates) + len(rejected),
        "repo_revision": revision,
        "repository_cache": str(root),
    }
    save_record(destination / "generation.json", result)
    return result


def evaluate(config: dict, destination: Path) -> dict:
    candidate = config["candidate"]
    profile = PythonRepositoryProfile.model_validate(
        {
            key: value
            for key, value in config["options"].items()
            if key in PythonRepositoryProfile.model_fields
        }
    )
    destination.mkdir(parents=True, exist_ok=False)
    repo = RepoSpec(url=candidate["repo"], ref=candidate["head"], access="public")
    boot, head = bootstrap_snapshot(repo, profile, destination)
    root = Path(config["repository_cache"])
    base = destination / "old"
    snapshot(root, candidate["base"], base)
    test_paths = []
    replacements = {}
    if config["recipe"] == "r2e_gym":
        suite = base / "r2e_tests"
        if suite.exists():
            raise ValueError("Input repository already owns reserved r2e_tests directory")
        suite.mkdir()
        (suite / "__init__.py").write_text("")
        for index, path in enumerate(candidate["test_files"]):
            shutil.copyfile(head / path, suite / f"test_{index + 1}.py")
        replacements["r2e_tests"] = suite
        test_paths = ["r2e_tests"]
    else:
        for path in candidate["test_files"]:
            target = base / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(head / path, target)
            replacements[path] = target
            test_paths.append(path)
    profile = profile.model_copy(update={"test_paths": test_paths})
    healthy = test_image(
        boot.image_digest, profile, destination / "healthy", replacements=replacements
    )
    broken = test_image(
        boot.image_digest,
        profile,
        destination / "defective",
        replacements={**replacements, **{path: base / path for path in candidate["source_files"]}},
    )
    contrast = execution_contrast(healthy, broken)
    result = {
        **candidate,
        "contrast": contrast,
        "profile": profile.model_dump(),
        "image_digest": boot.image_digest,
        "test_evidence": test_excerpts(base, contrast["FAIL_TO_PASS"]),
        "old_stdout": (destination / "defective/stdout.txt").read_text()[-10000:],
        "new_stdout": (destination / "healthy/stdout.txt").read_text()[-2000:],
    }
    save_record(destination / "candidate.json", result)
    return result


def main() -> None:
    if os.environ.get("REPO2RLENV_REMOTE_WORKER") != "1":
        raise RuntimeError("Historical repository execution is remote only")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    result = (
        evaluate(config, args.output)
        if config.get("mode") == "evaluate"
        else prepare(config, args.output)
    )
    console.json({"id": result.get("id"), "candidates": len(result.get("candidates", []))})


if __name__ == "__main__":
    main()
