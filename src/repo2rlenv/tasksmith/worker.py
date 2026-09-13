"""Remote-only deterministic Tasksmith stages; no provider or model credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import time
import traceback
from pathlib import Path, PurePosixPath

from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.execution.python_repository import bootstrap_snapshot, test_image
from repo2rlenv.pipelines.recipes.repository.export import (
    export_repository_task,
    private_asset,
    repository_build,
)
from repo2rlenv.quality.python_evidence import test_excerpts
from repo2rlenv.quality.test_results import execution_contrast
from repo2rlenv.spec.input import RepoSpec
from repo2rlenv.tasksmith.models import BootstrapHint, Design, Profile


def run(argv, *, cwd=None, timeout=120):
    result = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode:
        raise ValueError(
            f"Command {argv[0]} failed ({result.returncode}): {result.stderr[-6000:]}\n{result.stdout[-2000:]}"
        )
    return result.stdout


def inspect_source(source: dict, root: Path) -> dict:
    checkout = root / "checkout"
    checkout.mkdir()
    run(["git", "init", "-q", str(checkout)])
    run(["git", "-C", str(checkout), "fetch", "--depth=1", source["repo"], source["head"]])
    run(["git", "-C", str(checkout), "checkout", "--detach", "FETCH_HEAD"])
    if run(["git", "-C", str(checkout), "rev-parse", "HEAD"]).strip() != source["head"]:
        raise ValueError("Fetched source does not match the frozen head")
    return {"checkout": str(checkout), "head": source["head"]}


def dependency_image(profile: Profile, output: Path) -> dict:
    """Materialize a source-independent prefix, using the same Docker layer recipe as bootstrap."""
    return build_dependency_image(profile.options.base_image, profile.options.dependencies, output)


def build_dependency_image(base_image: str, dependencies: list[str], output: Path) -> dict:
    """Shared prefix for repository bootstrap and per-PR construction."""
    recipe = f"FROM {base_image}\nWORKDIR /workspace\n"
    if dependencies:
        recipe += f"RUN python -m pip install --no-cache-dir {shlex.join(dependencies)}\n"
    base = subprocess.run(
        ["docker", "image", "inspect", base_image, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if base.returncode:
        run(["docker", "pull", base_image], timeout=300)
        base_id = run(["docker", "image", "inspect", base_image, "--format", "{{.Id}}"])
    else:
        base_id = base.stdout
    key = hashlib.sha256((recipe + base_id.strip()).encode()).hexdigest()
    tag = "tasksmith-deps:" + key[:24]
    present = (
        subprocess.run(
            ["docker", "image", "inspect", tag], capture_output=True, check=False
        ).returncode
        == 0
    )
    if not present:
        context = output / "dependency-context"
        context.mkdir()
        (context / "Dockerfile").write_text(recipe)
        log = run(["docker", "build", "-t", tag, str(context)], timeout=600)
        (output / "dependency-build.stdout").write_text(log)
    image_id = run(["docker", "image", "inspect", tag, "--format", "{{.Id}}"]).strip()
    record = {
        "key": key,
        "image": tag,
        "image_id": image_id,
        "cache_hit": present,
        "scope": "provider-worker-lifetime",
        "recipe": recipe,
        "base_id": base_id.strip(),
    }
    save_record(output / "dependency-cache.json", record)
    return record


def bootstrap(source: dict, profile: Profile, output: Path) -> dict:
    cached = dependency_image(profile, output)
    boot, base = bootstrap_snapshot(
        RepoSpec(url=source["repo"], ref=source["head"], access="public"), profile.options, output
    )
    healthy = test_image(boot.image_digest, profile.options, output / "readiness")
    if healthy.returncode or not healthy.passed:
        raise ValueError(
            "Merged-head readiness failed; inspect readiness stdout/results before correcting the profile"
        )
    # Build the actual public workspace before spending on design or review.
    # Excluding a harmless-looking README can break the package's build backend.
    context = output / "public-context"
    public = context / "source"
    public.mkdir(parents=True)
    for path in base.rglob("*"):
        relative = PurePosixPath(path.relative_to(base).as_posix())
        if path.is_file() and not private_asset(relative, profile.options):
            destination = public / str(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    (context / "Dockerfile").write_text(repository_build(profile.options))
    public_tag = "tasksmith-public:" + hashlib.sha256(str(output).encode()).hexdigest()[:20]
    log = run(["docker", "build", "-t", public_tag, str(context)], timeout=600)
    (output / "public-build.stdout").write_text(log)
    shutil.rmtree(context)
    # Record the actual resolved environment; tasks retain the explicit install recipe.
    freeze = run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            boot.image_digest,
            "python",
            "-m",
            "pip",
            "freeze",
        ],
        timeout=60,
    )
    (output / "pip-freeze.txt").write_text(freeze)
    return {
        "image": boot.image_digest,
        "base": str(base),
        "readiness": healthy.statuses,
        "dependency_cache": cached,
        "public_image": public_tag,
    }


def construct(source: dict, profile: Profile, design: Design, ready: dict, output: Path) -> dict:
    base = Path(ready["base"])
    defective = output / "defective-source"
    defective.mkdir()
    for relative in source["source_files"]:
        path = defective / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(base / relative, path)
    patch = output / "source.diff"
    patch.write_text(source["source_diff"])
    run(["git", "apply", "--reverse", str(patch)], cwd=defective)
    options = profile.options.model_copy(deep=True)
    if design.upstream_test_policy == "replace":
        options.test_selectors = []
    additions = {}
    extra = "tests/tasksmith_behavior.py"
    if design.additional_tests.strip():
        if (base / extra).exists() or not (base / "tests").is_dir():
            raise ValueError(
                "Private supplements require an existing tests/ directory and an unused tasksmith_behavior.py path"
            )
        local = output / "tasksmith_behavior.py"
        local.write_text(design.additional_tests)
        additions[extra] = local
        options.test_selectors.append(extra)
        if "tests" not in options.test_paths:
            options.test_paths.append("tests")
    healthy = test_image(ready["image"], options, output / "healthy", replacements=additions)
    if healthy.returncode or not healthy.passed:
        raise ValueError(
            "Designed verifier fails on merged code; repair the verifier using healthy assertion logs"
        )
    broken = test_image(
        ready["image"],
        options,
        output / "defective",
        replacements={
            **additions,
            **{relative: defective / relative for relative in source["source_files"]},
        },
    )
    contrast = execution_contrast(healthy, broken)
    if not contrast["PASS_TO_PASS"]:
        raise ValueError("Include passing adjacent behavior to protect against regressions")
    task = export_repository_task(
        base=base,
        defective={
            relative: (defective / relative).read_bytes() for relative in source["source_files"]
        },
        reference={relative: (base / relative).read_bytes() for relative in source["source_files"]},
        options=options,
        instruction=design.instruction,
        destination=output / "task",
        name="tasksmith-" + source["id"],
        org="repo2rlenv",
        contrast=contrast,
        metadata={
            "recipe": "tasksmith",
            "recipe_version": "1",
            "source_url": source["url"],
            "source_head": source["head"],
            "source_base": source["base"],
            "workspace_strategy": source["workspace_strategy"],
            "source_diff_sha256": hashlib.sha256(source["source_diff"].encode()).hexdigest(),
            "acceptance_profile": "practical-generation-v1",
            "upstream_test_policy": design.upstream_test_policy,
        },
        verifier_source={relative: path.read_bytes() for relative, path in additions.items()},
    )
    return {
        "task": str(task),
        "task_relative": str(task.relative_to(output)),
        "contrast": contrast,
        "test_evidence": test_excerpts(
            base,
            contrast["FAIL_TO_PASS"],
            additional_sources={relative: path.read_text() for relative, path in additions.items()},
        ),
        "options": options.model_dump(),
        "strategy": design.strategy,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if os.environ.get("REPO2RLENV_REMOTE_WORKER") != "1":
        raise RuntimeError("Tasksmith target execution is remote only")
    args.output.mkdir(parents=True, exist_ok=False)
    data = json.loads(args.config.read_text())
    started = time.time()
    try:
        stage = data["stage"]
        if stage == "dependencies":
            hint = BootstrapHint.model_validate(data["hint"])
            value = build_dependency_image(hint.base_image, hint.dependencies, args.output)
        elif stage == "inspect":
            value = inspect_source(data["source"], args.output)
        elif stage == "bootstrap":
            value = bootstrap(data["source"], Profile.model_validate(data["profile"]), args.output)
        elif stage == "construct":
            value = construct(
                data["source"],
                Profile.model_validate(data["profile"]),
                Design.model_validate(data["design"]),
                data["ready"],
                args.output,
            )
        else:
            raise ValueError("Unknown remote stage")
        result = {"status": "completed", "value": value}
    except Exception as exc:
        logs = {
            str(path.relative_to(args.output)): path.read_text(errors="replace")[-8000:]
            for pattern in ("**/stdout.txt", "**/stderr.txt", "**/results.json")
            for path in args.output.glob(pattern)
        }
        result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "logs": logs}
        (args.output / "traceback.txt").write_text(traceback.format_exc())
    result["seconds"] = time.time() - started
    save_record(args.output / "stage-result.json", result)
    # The private inspection checkout is never downloaded. Build snapshots stay
    # on the worker; the exported task and compact evidence are sufficient.
    with tarfile.open(args.output.with_suffix(".tar.gz"), "w:gz") as archive:
        for path in sorted(args.output.iterdir()):
            if path.name not in {"base", "checkout"}:
                archive.add(path, arcname=args.output.name + "/" + path.name)


if __name__ == "__main__":
    main()
