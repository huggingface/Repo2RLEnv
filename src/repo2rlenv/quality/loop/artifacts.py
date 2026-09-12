"""Read-only task/evidence ingestion and transactional text repairs."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import tomllib
from pathlib import Path

import tomli_w

from repo2rlenv.emitter.bundle import inspect_bundle, relative_asset_path
from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.quality.loop.models import Repair, SemanticProbe, TrialRecord


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def task_identity(task: Path) -> str:
    identity = inspect_bundle(task)
    if identity["claimed_hash"] is not None and not identity["integrity_passed"]:
        raise ValueError("Task differs from its recorded bundle hash")
    return identity["bundle_hash"]


def parse_task(task: Path):
    try:
        from harbor.models.task.task import Task
    except ImportError as exc:
        raise ValueError("Install repo2rlenv[harbor] to review Harbor task definitions") from exc
    return Task(task)


def snapshot(task: Path, destination: Path) -> Path:
    identity = task_identity(task)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if task_identity(destination) != identity:
            raise ValueError("Existing snapshot differs from the source")
        return destination
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=destination.parent) as temporary:
        copied = Path(temporary) / task.name
        shutil.copytree(task, copied)
        if task_identity(copied) != identity:
            raise ValueError("Task changed during snapshot")
        copied.rename(destination)
    return destination


def refresh_identity(task: Path) -> str:
    config_path = task / "task.toml"
    config = tomllib.loads(config_path.read_text())
    metadata = config.get("metadata", {}).get("repo2env", {})
    if "bundle_hash" in metadata:
        metadata["bundle_hash"] = inspect_bundle(task)["bundle_hash"]
        config_path.write_text(tomli_w.dumps(config))
    return task_identity(task)


def edit_path(value: str) -> str:
    if value == "instruction.md":
        return value
    relative_asset_path(value)
    return value


def apply_repair(task: Path, repair: Repair, destination: Path) -> Path:
    """Apply exact replacements to a copy; no generated code executes locally."""
    task_identity(task)
    if destination.exists():
        raise FileExistsError(destination)
    if sum(len(edit.new) for edit in repair.edits) > 150000:
        raise ValueError("Repair exceeds the text-edit limit")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".repair-", dir=destination.parent) as temporary:
        copied = Path(temporary) / task.name
        shutil.copytree(task, copied)
        for edit in repair.edits:
            path = copied / edit_path(edit.path)
            if edit.old:
                text = path.read_text()
                if text.count(edit.old) != 1:
                    raise ValueError(f"Repair old text must occur exactly once: {edit.path}")
                path.write_text(text.replace(edit.old, edit.new, 1))
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("x") as handle:
                    handle.write(edit.new)
                path.chmod(0o755 if edit.executable else 0o644)
        if not (copied / "instruction.md").read_text().strip():
            raise ValueError("Repair erased the task instruction")
        new_hash = refresh_identity(copied)
        if new_hash == task_identity(task) and not repair.probe_replacements:
            raise ValueError("Repair did not change the task")
        copied.rename(destination)
    save_record(
        destination.parent / "repair.json",
        {
            "parent_hash": task_identity(task),
            "bundle_hash": new_hash,
            "repair": repair.model_dump(mode="json"),
        },
    )
    return destination


def probe_variant(task: Path, probe: SemanticProbe, destination: Path) -> Path:
    """Private reference-then-mutation control; instruction/tests/build stay exact."""
    if destination.exists():
        raise FileExistsError(destination)
    original = task / "solution/solve.sh"
    if not original.is_file():
        raise ValueError("Semantic probes require solution/solve.sh")
    snapshot(task, destination)
    private = destination / "solution/quality-original-solve.sh"
    if private.exists():
        raise ValueError("Task already uses the reserved probe filename")
    (destination / "solution/solve.sh").rename(private)
    wrapper = destination / "solution/solve.sh"
    wrapper.write_text(
        "#!/bin/bash\nset -eu\nbash /solution/quality-original-solve.sh\n"
        + probe.script
        + "\nprintf '__QUALITY_PROBE_COMPLETED__\\n'\n"
    )
    wrapper.chmod(0o755)
    identity = refresh_identity(destination)
    save_record(
        destination.parent / "probe.json",
        {
            "parent_hash": task_identity(task),
            "bundle_hash": identity,
            "probe": probe.model_dump(mode="json"),
        },
    )
    return destination


def import_trial(path: Path, task: Path, role: str) -> TrialRecord:
    """Import a receipt, trial result, or single-trial job; require content binding."""
    identity = task_identity(task)
    receipt = None
    if path.is_dir() and (path / "trial.json").is_file():
        path = path / "trial.json"
    if path.is_file() and path.name == "trial.json":
        receipt = json.loads(path.read_text())
        if receipt.get("state") != "completed" or receipt.get("bundle_hash") != identity:
            raise ValueError("Imported receipt is incomplete or belongs to a different task")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,60}", receipt.get("trial_id", "")):
            raise ValueError("Imported receipt has an invalid trial ID")
        candidates = list((path.parent / receipt["trial_id"]).glob("*/result.json"))
        if len(candidates) != 1:
            raise ValueError("Receipt must have exactly one adjacent Harbor trial result")
        result = candidates[0]
        if result.is_symlink() or not result.resolve().is_relative_to(path.parent.resolve()):
            raise ValueError("Imported receipt cannot redirect evidence outside its directory")
        if digest(result) != receipt["result_sha256"]:
            raise ValueError("Imported trial result changed after collection")
        binding = "receipt"
    else:
        candidates = (
            [path] if path.is_file() else [path / "result.json", *path.glob("*/result.json")]
        )
        results = []
        for candidate in candidates:
            if candidate.is_file():
                data = json.loads(candidate.read_text())
                if "task_checksum" in data:
                    results.append(candidate)
        if len(results) != 1:
            raise ValueError("Supply exactly one Harbor trial, not a multi-task job")
        result = results[0]
        if json.loads(result.read_text())["task_checksum"] != parse_task(task).checksum:
            raise ValueError("Harbor checksum does not match the supplied task")
        binding = "harbor_checksum"
    data = json.loads(result.read_text())
    exit_path = result.parent / "agent/exit-code.txt"
    agent_config = data.get("config", {}).get("agent", {})
    agent = agent_config.get("name") or agent_config.get("import_path") or "unknown"
    if role in {"baseline", "oracle"} and agent != {"baseline": "nop", "oracle": "oracle"}[role]:
        raise ValueError(f"Wrong agent for imported {role} evidence")
    if role == "rollout" and agent in {"nop", "oracle", "unknown"}:
        raise ValueError("A rollout must come from a solver, not a deterministic control")
    return TrialRecord(
        role=role,
        bundle_hash=identity,
        result=str(result.resolve()),
        result_sha256=digest(result),
        agent=agent,
        model=agent_config.get("model_name"),
        reward=(data.get("verifier_result") or {}).get("rewards", {}).get("reward"),
        exception_type=(data.get("exception_info") or {}).get("exception_type"),
        binding=binding,
        agent_exit_code=int(exit_path.read_text()) if exit_path.exists() else None,
    )
