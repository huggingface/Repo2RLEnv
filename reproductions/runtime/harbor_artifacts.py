"""Minimal Harbor packaging and independent execution receipts; run audits remotely."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from harbor.models.task.task import Task

TASK_ENTRIES = ("instruction.md", "task.toml", "environment", "tests", "solution")


def hashes(directory: Path) -> dict[str, str]:
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlink requires explicit asset review: {path}")
        if path.is_file():
            result[str(path.relative_to(directory))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def export_native(source: Path, destination: Path) -> dict:
    """Copy only Harbor contract entries; preserve native bytes and private evidence."""
    Task(source)
    if destination.exists():
        raise FileExistsError(destination)
    # Check before copying; do not dereference author-created host links.
    for entry in TASK_ENTRIES:
        path = source / entry
        if path.is_symlink():
            raise ValueError(f"Symlink requires explicit asset review: {path}")
        if path.is_dir():
            hashes(path)
    destination.mkdir(parents=True)
    for entry in TASK_ENTRIES:
        path = source / entry
        if path.is_dir():
            shutil.copytree(path, destination / entry)
        elif path.is_file():
            shutil.copy2(path, destination / entry)
    Task(destination)
    return {
        "source": str(source),
        "export": str(destination),
        "files": hashes(destination),
        "oracle_present": (destination / "solution" / "solve.sh").exists(),
        "conversion": "byte-preserving Harbor contract copy",
    }


def audit(task: Path, output: Path, agents: list[str]) -> dict:
    Task(task)
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "task": str(task),
        "task_hashes": hashes(task),
        "schema_valid": True,
        "runs": {},
        "execution_contrast_passed": False,
    }
    for agent in agents:
        if agent == "oracle" and not (task / "solution" / "solve.sh").is_file():
            report["runs"][agent] = {"status": "missing_upstream_oracle"}
            continue
        command = [
            "harbor",
            "run",
            "-p",
            str(task),
            "-a",
            agent,
            "-e",
            "docker",
            "-n",
            "1",
            "--max-retries",
            "0",
            "--jobs-dir",
            str(output),
            "--job-name",
            agent,
        ]
        with (output / f"{agent}.log").open("w") as log:
            try:
                process = subprocess.run(
                    command, stdout=log, stderr=subprocess.STDOUT, timeout=1800, check=False
                )
                returncode = process.returncode
            except subprocess.TimeoutExpired:
                returncode = None
        trials = []
        for result in (output / agent).glob("*/result.json"):
            data = json.loads(result.read_text())
            trials.append(
                {
                    "path": str(result),
                    "exception_info": data.get("exception_info"),
                    "verifier_result": data.get("verifier_result"),
                    "agent_result": data.get("agent_result"),
                }
            )
        report["runs"][agent] = {"command": command, "returncode": returncode, "trials": trials}
        (output / "audit.json").write_text(json.dumps(report, indent=2) + "\n")

    def reward(agent: str):
        run = report["runs"].get(agent, {})
        trials = run.get("trials", [])
        if run.get("returncode") != 0 or len(trials) != 1 or trials[0]["exception_info"]:
            return None
        rewards = (trials[0].get("verifier_result") or {}).get("rewards") or {}
        return rewards.get("reward")

    report["execution_contrast_passed"] = reward("nop") == 0 and reward("oracle") == 1
    report["note"] = (
        "Execution contrast alone is not test coverage, conversion parity or quality approval."
    )
    (output / "audit.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["export", "audit"])
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--agents", nargs="+", default=["nop", "oracle"], choices=["nop", "oracle"])
    args = parser.parse_args()
    if args.action == "export":
        report = export_native(args.source, args.destination)
        args.destination.with_suffix(".manifest.json").write_text(json.dumps(report, indent=2))
    else:
        report = audit(args.source, args.destination, args.agents)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
