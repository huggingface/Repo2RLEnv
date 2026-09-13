"""Deterministic reviewer evidence from captured commands and submitted source."""

from __future__ import annotations

import difflib
import json
from pathlib import Path

from repo2rlenv.quality.loop.artifacts import digest


def rollout_documents(task: Path, trial: Path) -> dict[str, str]:
    """Produce addressable documents; never infer edits from a solver's prose."""
    documents = {}
    trajectory = trial / "agent/trajectory.json"
    if not trajectory.resolve().is_relative_to(trial.resolve()):
        raise ValueError("Trial evidence cannot contain symlinks")
    if trajectory.is_file():
        if trajectory.is_symlink() or trajectory.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Rollout trajectory exceeds the regular-file evidence contract")
        data = json.loads(trajectory.read_text())
        records = [{"trajectory_sha256": digest(trajectory), "steps": len(data.get("steps", []))}]
        for step in data.get("steps", []):
            for call in step.get("tool_calls", []):
                records.append({"step_id": step.get("step_id"), **call})
        documents["tool-calls.jsonl"] = "\n".join(json.dumps(row) for row in records) + "\n"
    manifest = trial / "artifacts/manifest.json"
    if not manifest.resolve().is_relative_to(trial.resolve()):
        raise ValueError("Trial evidence cannot contain symlinks")
    if not manifest.is_file():
        return documents
    if manifest.is_symlink() or manifest.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Artifact manifest exceeds the regular-file evidence contract")
    changes, patches = [], []
    for entry in json.loads(manifest.read_text()):
        source = entry.get("source", "")
        if not source.startswith("/workspace/") or entry.get("service") not in {None, "main"}:
            continue
        relative = Path(source.removeprefix("/workspace/"))
        destination = Path(entry["destination"])
        if (
            relative.is_absolute()
            or destination.is_absolute()
            or ".." in (*relative.parts, *destination.parts)
        ):
            raise ValueError("Rollout artifact path escapes its evidence root")
        captured = trial / destination
        baseline = task / "environment/source" / relative
        if entry["status"] != "ok":
            changes.append({"path": str(relative), "status": "collection_" + entry["status"]})
            continue
        names = {Path(".")}
        if entry["type"] == "directory":
            names = {p.relative_to(captured) for p in captured.rglob("*.py")}
            names |= {p.relative_to(baseline) for p in baseline.rglob("*.py")}
        for name in sorted(names):
            before, after = baseline / name, captured / name
            if entry["type"] == "file":
                before, after = baseline, captured
            if before.suffix != ".py":
                continue
            for path, root in ((before, task), (after, trial)):
                if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError("Rollout source contains a linked or escaping path")
                if path.exists() and (not path.is_file() or path.stat().st_size > 2 * 1024 * 1024):
                    raise ValueError("Rollout source exceeds the bounded text contract")
            old = before.read_bytes() if before.exists() else b""
            new = after.read_bytes() if after.exists() else b""
            if old == new and before.exists() == after.exists():
                continue
            path = (
                (relative / name).as_posix()
                if entry["type"] == "directory"
                else relative.as_posix()
            )
            changes.append(
                {
                    "path": path,
                    "before_sha256": digest(before) if before.exists() else None,
                    "after_sha256": digest(after) if after.exists() else None,
                }
            )
            patches.extend(
                difflib.unified_diff(
                    old.decode().splitlines(keepends=True),
                    new.decode().splitlines(keepends=True),
                    fromfile="baseline/" + path,
                    tofile="submitted/" + path,
                )
            )
    documents["submission-index.json"] = json.dumps(
        {"manifest_sha256": digest(manifest), "changes": changes}, indent=2
    )
    documents["submitted-source.diff"] = (
        "".join(patches) or "[No captured Python source differences]\n"
    )
    return documents
