"""Immutable Harbor release staging and receipt-backed Hugging Face publication.

Only explicitly selected task bundles enter the release. Generation directories,
model requests, credentials and worker receipts are never traversed for upload.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
import tomllib
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from repo2rlenv.emitter.bundle import inspect_bundle
from repo2rlenv.execution.lifecycle import now, save_record


class ReleaseTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: Path
    bundle_hash: str
    evidence: dict = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list)


class ReleasePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo_id: str
    recipe: str
    title: str
    description: str
    methodology: str
    code_revision: str
    tasks: list[ReleaseTask] = Field(min_length=1)
    economics: dict = Field(default_factory=dict)
    citations: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @field_validator("repo_id")
    @classmethod
    def valid_repo(cls, value):
        from huggingface_hub.utils import validate_repo_id

        validate_repo_id(value)
        if value.count("/") != 1:
            raise ValueError("Release repo_id requires an explicit owner")
        return value


def _files(root: Path) -> dict:
    records = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Release files cannot be symbolic links")
        if not path.is_file():
            continue
        records[path.relative_to(root).as_posix()] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": path.stat().st_mode & 0o777,
            "bytes": path.stat().st_size,
        }
    return records


def stage_release(plan: ReleasePlan, destination: Path) -> dict:
    """Check exact identities, copy artifacts and create a mode-preserving archive."""
    if destination.exists():
        raise FileExistsError("Release staging already exists; verify it instead of overwriting")
    names = [task.path.name for task in plan.tasks]
    if len(set(names)) != len(names):
        raise ValueError("A release cannot contain duplicate task IDs")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".release-", dir=destination.parent))
    try:
        rows = []
        for selected in plan.tasks:
            identity = inspect_bundle(selected.path)
            if not identity["integrity_passed"] or identity["bundle_hash"] != selected.bundle_hash:
                raise ValueError(f"Task content changed: {selected.path.name}")
            from harbor.models.task.task import Task

            Task(selected.path)
            target = temporary / "tasks" / selected.path.name
            shutil.copytree(selected.path, target)
            copied = inspect_bundle(target)
            if copied != identity:
                raise ValueError("Task changed during release staging")
            metadata = tomllib.loads((target / "task.toml").read_text())["metadata"]["repo2env"]
            if metadata.get("recipe", "").replace("_", "-") != plan.recipe.replace("_", "-"):
                raise ValueError("Task recipe does not match release")
            rows.append(
                {
                    "task_id": target.name,
                    "path": "tasks/" + target.name,
                    "bundle_hash": identity["bundle_hash"],
                    "quality_status": metadata.get("quality_status", "unknown"),
                    "metadata": metadata,
                    "evidence": selected.evidence,
                    "diagnostics": selected.diagnostics,
                }
            )
        file_manifest = _files(temporary / "tasks")
        manifest = {
            "schema_version": 1,
            "repo_id": plan.repo_id,
            "recipe": plan.recipe,
            "created_at": now(),
            "code_revision": plan.code_revision,
            "task_count": len(rows),
            "tasks": rows,
            "economics": plan.economics,
            "citations": plan.citations,
            "limitations": plan.limitations,
            "quality_counts": dict(Counter(row["quality_status"] for row in rows)),
        }
        save_record(temporary / "manifest.json", manifest)
        save_record(temporary / "bundle-files.json", {"version": 1, "files": file_manifest})
        data = temporary / "data"
        data.mkdir()
        with (data / "tasks.jsonl").open("w") as stream:
            for row in rows:
                stream.write(
                    json.dumps(
                        {
                            "task_id": row["task_id"],
                            "recipe": plan.recipe,
                            "quality_status": row["quality_status"],
                            "bundle_hash": row["bundle_hash"],
                            "task_path": row["path"],
                            "instruction": (temporary / row["path"] / "instruction.md").read_text(),
                            "evidence_json": json.dumps(row["evidence"], sort_keys=True),
                            "diagnostics": row["diagnostics"],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        with tarfile.open(temporary / "tasks.tar.gz", "w:gz") as archive:
            archive.add(temporary / "tasks", arcname="tasks")
        (temporary / "README.md").write_text(_card(plan, manifest))
        (temporary / "LICENSES.md").write_text(
            "# Source licenses and attribution\n\n"
            "This is a mixed-source research collection. Repository files and attributed "
            "source material retain their original licenses; this release does not relicense "
            "them. Inspect each task for bundled LICENSE/COPYING notices and source provenance "
            "in task.toml and manifest.json. Stack Exchange excerpts retain their recorded "
            "CC BY-SA version and author attribution. A source with no explicit license is "
            "not represented as freely relicensed.\n\n"
            + "\n".join("- " + link for link in plan.citations)
            + "\n"
        )
        save_record(
            temporary / "release-files.json",
            {"version": 1, "repo_id": plan.repo_id, "files": _files(temporary)},
        )
        temporary.rename(destination)
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _card(plan: ReleasePlan, manifest: dict) -> str:
    import yaml

    front = yaml.safe_dump(
        {
            "license": "other",
            "license_name": "mixed-source-licenses",
            "license_link": "LICENSES.md",
            "language": ["en"],
            "tags": ["reinforcement-learning", "coding", "harbor", "repo2rlenv", plan.recipe],
            "size_categories": ["n<1K"],
            "configs": [
                {
                    "config_name": "default",
                    "data_files": [{"split": "train", "path": "data/tasks.jsonl"}],
                }
            ],
        },
        sort_keys=False,
    )
    limitations = "\n".join("- " + value for value in plan.limitations)
    references = "\n".join("- " + value for value in plan.citations)
    return f"""---
{front}---

# {plan.title}

{plan.description}

Contains **{manifest["task_count"]} Harbor tasks** generated with the owned
`{plan.recipe}` recipe in [Repo2RLEnv](https://github.com/huggingface/Repo2RLEnv).
The task bundle is under `tasks/<task_id>/`; `data/tasks.jsonl` is a browsing index.
`manifest.json` records source identity, evidence, diagnostics and measured costs.

## Generation

{plan.methodology}

Implementation revision: `{plan.code_revision}`. The exact recipe, source revision,
reward kinds and quality status remain in each original `task.toml`.

## Validation and limitations

Quality label counts: `{json.dumps(manifest["quality_counts"], sort_keys=True)}`.
An `exported` task is a generation artifact. Baseline/reference controls establish
only the behavior recorded in that task's evidence. They do not establish blind
solver success, difficulty, verifier completeness or resistance to reward hacking.
An LLM consistency review is separate from the deterministic task reward.

{limitations}

## Download and run

`tasks.tar.gz` preserves executable file modes and the original bundle identities:

```bash
hf download {plan.repo_id} tasks.tar.gz --repo-type dataset --local-dir ./dataset
tar -xzf ./dataset/tasks.tar.gz -C ./dataset
harbor run --path ./dataset/tasks --agent oracle --env daytona
```

Configure Daytona credentials and Harbor's provider dependencies before execution.
The archive includes references and private tests for the harness; the solver
should receive only the instruction and learner environment. `registry.json`
pins the unpacked task paths to the immutable upload commit.

## Economics

`manifest.json` includes generation costs, failed-attempt costs and outstanding
reservations when available. Cloud lifetime estimates are labeled separately from
provider invoices. Retained task costs and new-generation costs use separate scopes.

## Credits and licensing

{references}

See [LICENSES.md](LICENSES.md), bundled notices and per-task provenance. These are
owned adaptations inspired by the credited methods, not an upstream benchmark
release or a claim of exact reproduction of its published results.
"""


def verify_release(directory: Path) -> dict:
    expected = json.loads((directory / "release-files.json").read_text())
    actual = _files(directory)
    actual.pop("release-files.json", None)
    if actual != expected["files"]:
        raise ValueError("Staged release changed; create a new immutable staging directory")
    return expected


def publish_release(
    directory: Path, *, api, receipt: Path, collection_slug: str | None = None
) -> dict:
    """Publish a verified snapshot; retry uncertain effects only after reconciliation."""
    from repo2rlenv.hub import _build_registry_json

    files = verify_release(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    identity = hashlib.sha256((directory / "release-files.json").read_bytes()).hexdigest()
    if receipt.exists():
        record = json.loads(receipt.read_text())
        if record["release_sha256"] != identity:
            raise ValueError("Publication receipt belongs to another release")
        if record["state"] == "completed":
            return record
        raise ValueError("Publication needs reconciliation; no automatic upload replay")
    repo_id = files["repo_id"]
    record = {
        "state": "prepared",
        "repo_id": repo_id,
        "release_sha256": identity,
        "created_at": now(),
        "collection_slug": collection_slug,
    }
    save_record(receipt, record)
    api.create_repo(repo_id, repo_type="dataset", private=False, exist_ok=True)
    record["state"] = "upload_dispatched"
    save_record(receipt, record)
    commit = api.upload_folder(
        repo_id=repo_id,
        repo_type="dataset",
        folder_path=str(directory),
        commit_message=f"Publish {manifest['task_count']} {manifest['recipe']} Harbor tasks",
    )
    record.update(state="uploaded", commit_sha=commit.oid)
    save_record(receipt, record)
    registry = _build_registry_json(
        repo_id,
        commit.oid,
        repo_id.split("/")[1],
        manifest["recipe"] + " Harbor tasks",
        [row["task_id"] for row in manifest["tasks"]],
    )
    api.upload_file(
        repo_id=repo_id,
        repo_type="dataset",
        path_in_repo="registry.json",
        path_or_fileobj=json.dumps(registry, indent=2).encode(),
        commit_message="Pin Harbor registry to the release commit",
    )
    remote_paths = set(api.list_repo_files(repo_id, repo_type="dataset", revision=commit.oid))
    required = {row["path"] + "/task.toml" for row in manifest["tasks"]} | {
        "manifest.json",
        "tasks.tar.gz",
        "data/tasks.jsonl",
    }
    if not required <= remote_paths:
        raise RuntimeError("Published revision is missing release files")
    if collection_slug:
        api.add_collection_item(
            collection_slug,
            item_id=repo_id,
            item_type="dataset",
            exists_ok=True,
            note=f"{manifest['task_count']} Harbor tasks; see per-task quality labels and evidence.",
        )
    record.update(
        state="completed",
        finished_at=now(),
        task_count=manifest["task_count"],
        url="https://huggingface.co/datasets/" + repo_id,
    )
    save_record(receipt, record)
    return record
