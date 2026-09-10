"""Unified experimental runner for pinned upstream reproductions.

Recipes execute only on a named remote worker. This controller records stage
identity and reserves allowance; upstream limits still bound model execution.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from runtime.budget import now, reserve

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def named(value: str) -> str:
    if not SAFE_NAME.fullmatch(value):
        raise ValueError(f"Invalid experiment, stage, worker or attempt name: {value!r}")
    return value


def workflow(experiment: str) -> dict:
    path = ROOT / named(experiment) / "workflow.json"
    if not path.exists():
        raise ValueError(f"{experiment}: source catalogued, execution recipes not ready")
    result = json.loads(path.read_text())
    if result["id"] != experiment:
        raise ValueError("Workflow identity mismatch")
    for stage, spec in result["stages"].items():
        named(stage)
        recipe = (ROOT / spec["script"]).resolve()
        if not recipe.is_relative_to(ROOT) or not recipe.is_file():
            raise ValueError(f"Invalid recipe for {experiment}/{stage}")
        if not 1 <= spec["timeout_seconds"] <= 14400:
            raise ValueError("Stage timeout must be bounded by the worker's maximum lifetime")
        if not set(spec.get("credentials", [])) <= {
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "HF_TOKEN",
            "GITHUB_TOKEN",
        }:
            raise ValueError("Unsupported credential name")
    return result


def journal_path(worker: str, experiment: str, stage: str, attempt: str) -> Path:
    return (
        ROOT
        / "runs/runner"
        / named(worker)
        / named(experiment)
        / (named(stage) + "-" + named(attempt) + ".json")
    )


def invoke(arguments: list[str], log=None) -> None:
    subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT.parent,
        check=True,
        stdout=log,
        stderr=subprocess.STDOUT if log else None,
    )


def recipe_manifest(experiment: str) -> dict[str, str]:
    result = {}
    for directory in dict.fromkeys([ROOT / "runtime", ROOT / experiment]):
        for path in directory.rglob("*"):
            relative = path.relative_to(ROOT)
            if any(
                part in {"upstream", "runs", "harbor", "data", "cache", ".venv", "__pycache__"}
                for part in relative.parts
            ):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            if path.suffix in {".py", ".sh", ".patch", ".yaml", ".toml", ".in"} or path.name in {
                "requirements.txt",
                "requirements.lock.txt",
                "workflow.json",
                "experiment.json",
            }:
                result[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def completed_dependency(worker: str, dependency: str) -> dict:
    experiment, stage = dependency.split("/", 1)
    directory = journal_path(worker, experiment, stage, "lookup").parent
    spec = workflow(experiment)["stages"][stage]
    digest = hashlib.sha256((ROOT / spec["script"]).read_bytes()).hexdigest()
    candidates = []
    for path in directory.glob(named(stage) + "-*.json"):
        receipt = json.loads(path.read_text())
        if (
            receipt["status"] == "completed"
            and receipt["script_sha256"] == digest
            and receipt["worker"] == worker
        ):
            candidates.append((receipt["finished_at"], path, receipt))
    if not candidates:
        raise ValueError(f"First complete {dependency} with the current recipe on {worker}")
    _, path, receipt = max(candidates, key=lambda entry: entry[0])
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "attempt": receipt["attempt"],
    }


def execute(experiment: str, stage: str, worker: str, attempt: str) -> dict:
    flow = workflow(experiment)
    if stage not in flow["stages"]:
        raise ValueError(f"Unknown stage {stage}; choose from {list(flow['stages'])}")
    spec = flow["stages"][stage]
    dependencies = {dep: completed_dependency(worker, dep) for dep in spec.get("requires", [])}
    worker_receipt = json.loads((ROOT / "runs" / (named(worker) + ".json")).read_text())
    if worker_receipt["state"] != "running":
        raise ValueError("The selected worker is not running")
    recipe = ROOT / spec["script"]
    path = journal_path(worker, experiment, stage, attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Claim before any remote call or reservation. Interrupted states require an
    # explicit new attempt after inspection; never silently rerun a paid stage.
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            raise ValueError(f"Stage already recorded at {path}; inspect it before retrying")
        record = {
            "experiment": experiment,
            "stage": stage,
            "worker": worker,
            "attempt": attempt,
            "script": spec["script"],
            "script_sha256": hashlib.sha256(recipe.read_bytes()).hexdigest(),
            "stage_config": spec,
            "recipe_files": recipe_manifest(experiment),
            "dependencies": dependencies,
            "started_at": now(),
            "status": "claimed",
            "reservation_key": f"runner:{worker}:{experiment}:{stage}:{attempt}",
        }
        path.write_text(json.dumps(record, indent=2) + "\n")
    try:
        amount = spec.get("reserve_usd")
        if amount:
            reserve(
                record["reservation_key"],
                str(amount),
                f"{experiment}/{stage}: {spec['description']}",
            )
        invoke([str(RUNTIME / "sync_recipes.py"), "--name", worker, "runtime", experiment])
        if experiment == "runtime":
            invoke(
                [
                    str(RUNTIME / "modal_worker.py"),
                    "upload",
                    "--name",
                    worker,
                    "--local",
                    str(ROOT / "artifacts.json"),
                    "--remote",
                    "/work/recipes/artifacts.json",
                ]
            )
        arguments = [
            str(RUNTIME / "modal_worker.py"),
            "exec",
            "--name",
            worker,
            "--script",
            str(recipe),
            "--timeout",
            str(spec["timeout_seconds"]),
        ]
        for key in spec.get("credentials", []):
            arguments += ["--credential", key]
        record.update(status="running", log=str(path.with_suffix(".log")))
        path.write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(record), flush=True)
        with path.with_suffix(".log").open("w") as log:
            invoke(arguments, log=log)
        record["status"] = "completed"
    except BaseException as error:
        record.update(status="failed_or_interrupted", error_type=type(error).__name__)
        raise
    finally:
        record["finished_at"] = now()
        path.write_text(json.dumps(record, indent=2) + "\n")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    plan = actions.add_parser("plan")
    plan.add_argument("experiment")
    run = actions.add_parser("run")
    run.add_argument("experiment")
    run.add_argument("stage")
    run.add_argument("--worker", required=True)
    run.add_argument("--attempt", default="smoke01")
    args = parser.parse_args()
    if args.action == "list":
        index = json.loads((ROOT / "index.json").read_text())
        names = ["runtime", *index["generation_order"], *index["supporting_components"]]
        for name in names:
            path = ROOT / name / "workflow.json"
            print(
                json.dumps(
                    {
                        "id": name,
                        "runnable_stages": list(workflow(name)["stages"]) if path.exists() else [],
                    }
                )
            )
    elif args.action == "plan":
        print(json.dumps(workflow(args.experiment), indent=2))
    else:
        print(json.dumps(execute(args.experiment, args.stage, args.worker, args.attempt), indent=2))


if __name__ == "__main__":
    main()
