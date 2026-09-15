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
    task_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    bundle_hash: str
    evidence: dict = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list)

    @field_validator("task_id")
    @classmethod
    def valid_task_id(cls, value):
        if value is not None and ".." in value:
            raise ValueError("Task IDs cannot contain '..'")
        return value


class ReleasePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo_id: str
    recipe: str
    title: str
    description: str
    methodology: str
    code_revision: str
    tasks: list[ReleaseTask] = Field(min_length=1)
    normalize_evaluation_labels: bool = False
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
    sources = [task.path.resolve() for task in plan.tasks]
    if any(destination.resolve().is_relative_to(source) for source in sources):
        raise ValueError("Release staging cannot be inside a selected task")
    names = [task.task_id or source.name for task, source in zip(plan.tasks, sources, strict=True)]
    if len(set(names)) != len(names):
        raise ValueError("A release cannot contain duplicate task IDs")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".release-", dir=destination.parent))
    try:
        rows = []
        for selected, name in zip(plan.tasks, names, strict=True):
            identity = inspect_bundle(selected.path)
            if not identity["integrity_passed"] or identity["bundle_hash"] != selected.bundle_hash:
                raise ValueError(f"Task content changed: {selected.path.name}")
            from harbor.models.task.task import Task

            task = Task(selected.path)
            if selected.task_id and task.config.task.name.split("/")[-1] != selected.task_id:
                raise ValueError("Explicit task ID must match the Harbor package name")
            target = temporary / "tasks" / name
            source_metadata = tomllib.loads((selected.path / "task.toml").read_text())["metadata"][
                "repo2env"
            ]
            normalized = plan.normalize_evaluation_labels and "evaluation" not in source_metadata
            if normalized:
                from repo2rlenv.emitter.evaluation import EvaluationLabel
                from repo2rlenv.quality.labels import write_labeled_copy

                write_labeled_copy(
                    selected.path,
                    target,
                    EvaluationLabel(
                        subject_bundle_hash=identity["bundle_hash"],
                        reason_codes=["independent_validation_incomplete"],
                        detail=(
                            "Generation export; independent quality acceptance has not been "
                            "established. See release evidence for the recorded native checks."
                        ),
                    ),
                )
            else:
                shutil.copytree(selected.path, target)
            copied = inspect_bundle(target)
            if copied != identity:
                raise ValueError("Task changed during release staging")
            metadata = tomllib.loads((target / "task.toml").read_text())["metadata"]["repo2env"]
            if metadata.get("recipe", "").replace("_", "-") != plan.recipe.replace("_", "-"):
                raise ValueError("Task recipe does not match release")
            status = metadata.get("quality_status", "unknown")
            if "evaluation" in metadata:
                from repo2rlenv.emitter.evaluation import EvaluationLabel

                label = EvaluationLabel.model_validate(metadata["evaluation"])
                if label.subject_bundle_hash not in {None, identity["bundle_hash"]}:
                    raise ValueError("Evaluation label belongs to a different task revision")
                status = label.status
            rows.append(
                {
                    "task_id": target.name,
                    "path": "tasks/" + target.name,
                    "bundle_hash": identity["bundle_hash"],
                    "quality_status": status,
                    "generation_status": metadata.get("quality_status", "unknown"),
                    "evaluation_label_normalized": normalized,
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
            "license_link": f"https://huggingface.co/datasets/{plan.repo_id}/blob/main/LICENSES.md",
            "language": ["en"],
            "tags": ["reinforcement-learning", "coding", "harbor", "repo2rlenv", plan.recipe],
            "size_categories": ["n<1K"],
            "viewer": False,
        },
        sort_keys=False,
    )
    limitations = "\n".join("- " + value for value in plan.limitations)
    references = "\n".join("- " + value for value in plan.citations)
    viewer = f"https://huggingface.co/spaces/HuggingFaceH4/harbor-visualiser?dataset={plan.repo_id}"
    task_tree = f"https://huggingface.co/datasets/{plan.repo_id}/tree/main/tasks"
    example = manifest["tasks"][0]["path"]
    example_file = f"https://huggingface.co/datasets/{plan.repo_id}/blob/main/{example}"
    return f"""---
{front}---

[![View tasks in Harbor Visualiser](https://img.shields.io/badge/Harbor_Visualiser-View_tasks-FFD21F?style=for-the-badge)]({viewer})

# {plan.title}

{plan.description}

Contains **{manifest["task_count"]} Harbor tasks** generated with the owned
`{plan.recipe}` recipe in [Repo2RLEnv](https://github.com/huggingface/Repo2RLEnv).
Browse the complete task bundles in [Harbor Visualiser]({viewer}) or
[open the task folders]({task_tree}). Each folder is a runnable Harbor task:

```text
tasks/<task_id>/
├── task.toml                 # Harbor configuration and provenance
├── instruction.md            # Task shown to the coding agent
├── environment/Dockerfile    # Learner sandbox and its build context
├── solution/solve.sh         # Reference solution entry point
└── tests/
    ├── test.sh               # Verifier entry point; writes the reward
    └── Dockerfile            # Separate verifier sandbox, when configured
```

Example: [task.toml]({example_file}/task.toml) ·
[instruction]({example_file}/instruction.md) ·
[verifier]({example_file}/tests/test.sh) ·
[oracle]({example_file}/solution/solve.sh).

`data/tasks.jsonl` is an auxiliary metadata index. Download the task folders or
archive below to run the environments. `manifest.json` records source identity,
evidence, diagnostics and measured costs. The generic tabular Hub viewer is
disabled so it does not present the index as the task dataset.

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
    directory: Path,
    *,
    api,
    receipt: Path,
    collection_slug: str | None = None,
    batch_size: int | None = None,
) -> dict:
    """Publish a verified snapshot; retry uncertain effects only after reconciliation."""
    if batch_size is not None and not 1 <= batch_size <= 500:
        raise ValueError("Publication batches must contain one to 500 files")
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
    if batch_size is not None:
        return _upload_empty_repository(
            directory,
            api=api,
            receipt=receipt,
            record=record,
            files=files,
            batch_size=batch_size,
        )
    commit = api.upload_folder(
        repo_id=repo_id,
        repo_type="dataset",
        folder_path=str(directory),
        commit_message=f"Publish {manifest['task_count']} {manifest['recipe']} Harbor tasks",
    )
    record.update(state="uploaded", commit_sha=commit.oid)
    save_record(receipt, record)
    return _finish_publish(directory, api=api, receipt=receipt, record=record)


def _finish_publish(directory: Path, *, api, receipt: Path, record: dict) -> dict:
    from repo2rlenv.hub import _build_registry_json

    manifest = json.loads((directory / "manifest.json").read_text())
    repo_id = record["repo_id"]
    commit_sha = record["commit_sha"]
    collection_slug = record.get("collection_slug")
    remote_paths = set(api.list_repo_files(repo_id, repo_type="dataset", revision=commit_sha))
    release_files = json.loads((directory / "release-files.json").read_text())
    required = set(release_files["files"]) | {"release-files.json"}
    missing = required - remote_paths
    if missing:
        raise RuntimeError(
            f"Published revision is missing {len(missing)} release files: "
            + ", ".join(sorted(missing)[:5])
        )
    registry = _build_registry_json(
        repo_id,
        commit_sha,
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


def recover_empty_upload(directory: Path, *, api, receipt: Path, batch_size: int = 250) -> dict:
    """Recover a rejected/timed-out upload only after confirming no artifact commit.

    Large atomic commits can time out at the Hub gateway. This explicit recovery
    writes bounded commits with parent guards; uncertain chunks remain recorded
    for inspection. It never overwrites a nonempty repository or retries a chunk.
    """
    if not 1 <= batch_size <= 500:
        raise ValueError("Publication batches must contain one to 500 files")
    files = verify_release(directory)
    record = json.loads(receipt.read_text())
    identity = hashlib.sha256((directory / "release-files.json").read_bytes()).hexdigest()
    if record["release_sha256"] != identity or record["repo_id"] != files["repo_id"]:
        raise ValueError("Publication receipt belongs to another release")
    if record["state"] != "upload_dispatched":
        raise ValueError("Only an unconfirmed original upload can use empty-repository recovery")
    return _upload_empty_repository(
        directory, api=api, receipt=receipt, record=record, files=files, batch_size=batch_size
    )


def _upload_empty_repository(
    directory: Path, *, api, receipt: Path, record: dict, files: dict, batch_size: int
) -> dict:
    from huggingface_hub import CommitOperationAdd

    repo_id = record["repo_id"]
    parent = api.repo_info(repo_id, repo_type="dataset").sha
    remote = set(api.list_repo_files(repo_id, repo_type="dataset", revision=parent))
    if remote - {".gitattributes"}:
        raise ValueError(
            "Remote artifacts exist; inspect the committed release instead of replaying"
        )
    record.update(state="batch_upload", reconciled_empty_commit=parent, batches=[])
    save_record(receipt, record)
    paths = sorted(set(files["files"]) - {"README.md"})
    # Publish the release identity after its artifact files, as the completion marker.
    paths.extend(["README.md", "release-files.json"])
    for offset in range(0, len(paths), batch_size):
        selected = paths[offset : offset + batch_size]
        batch = {"state": "dispatched", "parent_commit": parent, "paths": selected}
        record["batches"].append(batch)
        save_record(receipt, record)
        commit = api.create_commit(
            repo_id=repo_id,
            repo_type="dataset",
            parent_commit=parent,
            operations=[
                CommitOperationAdd(path_in_repo=name, path_or_fileobj=directory / name)
                for name in selected
            ],
            commit_message=f"Stage Harbor release files {offset + 1}-{offset + len(selected)}",
        )
        parent = commit.oid
        batch.update(state="completed", commit_sha=parent)
        save_record(receipt, record)
    record.update(state="uploaded", commit_sha=parent)
    save_record(receipt, record)
    return _finish_publish(directory, api=api, receipt=receipt, record=record)
