"""Bind reusable generations to their original run and frozen PR evidence."""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

from repo2rlenv.quality.loop.artifacts import task_identity
from repo2rlenv.quality.loop.models import SemanticProbe
from repo2rlenv.tasksmith.models import Panel


def generation_task(record: dict, source: dict, directory: Path) -> dict | None:
    if record.get("id") != source["id"]:
        raise ValueError("Saved candidate belongs to a different PR")
    quality = record.get("quality", {})
    if quality:
        task = Path(quality["task_path"])
        expected = quality["bundle_hash"]
    elif record.get("constructed"):
        built = record["constructed"]
        task = Path(built["local"]) / built["value"]["task_relative"]
        expected = None
    else:
        return None
    task = task.resolve()
    if not task.is_relative_to(directory.resolve()):
        raise ValueError("Saved generation points outside its run directory")
    metadata = tomllib.loads((task / "task.toml").read_text())["metadata"]["repo2env"]
    binding = {
        "recipe": "tasksmith",
        "recipe_version": "1",
        "source_url": source["url"],
        "source_head": source["head"],
        "source_base": source["base"],
        "source_diff_sha256": hashlib.sha256(source["source_diff"].encode()).hexdigest(),
        "workspace_strategy": source["workspace_strategy"],
    }
    if any(metadata.get(key) != value for key, value in binding.items()):
        raise ValueError("Saved generation metadata differs from the frozen PR")
    identity = task_identity(task)
    if expected is not None and identity != expected:
        raise ValueError("Imported task differs from its saved evidence")
    # A draft whose requirements conflict with the frozen reference needs fresh
    # design. Its probes were judged against that invalid draft; retain them in
    # the original run rather than treating them as proven PR counterexamples.
    issues = (quality.get("review") or {}).get("issues", [])
    if any(
        item.get("category") == "reference" and item.get("severity") == "blocking"
        for item in issues
    ):
        return None
    retained = {}
    for trial in quality.get("trials", []):
        if trial.get("role") == "probe" and trial.get("probe"):
            probe = SemanticProbe.model_validate(trial["probe"])
            if probe.name in retained and retained[probe.name] != probe.model_dump():
                raise ValueError("Saved probes disagree on the same control identity")
            retained[probe.name] = probe.model_dump()
    return {
        "task": str(task),
        "bundle_hash": identity,
        "generation_run": str(directory.resolve()),
        "probes": list(retained.values()),
    }


def load_generation(directory: Path, panel: Panel) -> tuple[list[dict], dict]:
    previous = json.loads((directory / "panel.json").read_text())
    if previous["configuration"]["panel"] != panel.model_dump():
        raise ValueError("Imported generation must have the identical frozen panel")
    sources = previous["sources"]
    if [source["url"] for source in sources] != [url.rstrip("/") for url in panel.prs]:
        raise ValueError("Imported source identities differ from the frozen panel")
    imported = {}
    for source in sources:
        candidate = directory / "candidates" / source["id"] / "result.json"
        if not candidate.resolve().is_relative_to(directory.resolve()):
            raise ValueError("Saved candidate points outside its run directory")
        if not candidate.is_file():
            continue
        value = generation_task(json.loads(candidate.read_text()), source, directory)
        if value is not None:
            imported[source["id"]] = value
    return sources, imported
