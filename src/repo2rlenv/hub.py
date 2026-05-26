"""HF Hub push/pull for Repo2RLEnv datasets.

Generates a Harbor-compatible registry.json at the dataset root pointing at
the HF dataset's git URL + commit SHA, plus a friendly README dataset card.
Anyone can then `harbor download <task> --registry-url <hf-resolve-url>` to
pull tasks; this is verified to round-trip through `harbor`'s standard
git-clone path.

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
The `registry.json` format used here is Harbor's legacy registry format,
documented at:

  Harbor Framework (Laude Institute / Terminal-Bench creators)
  https://github.com/harbor-framework/harbor    (Apache-2.0)

We chose this format because (a) Harbor's existing `download` CLI parses it
out of the box (we tested), (b) HF Hub datasets are git repos, so an HF
dataset can serve registry.json AND host the task git references — closing
the loop with zero Harbor patches. We do NOT depend on the harbor Python
package; this module uses only `huggingface_hub` and stdlib JSON.

Released under Apache-2.0.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repo2rlenv.auth import resolve_hf_token
from repo2rlenv.spec.input import AuthSpec

logger = logging.getLogger(__name__)


# Harbor Visualiser Space — the public viewer that renders each pushed dataset's
# task spec in the browser. Override with `R2E_VISUALISER_URL` env var to point
# at a fork / dev deployment.
VISUALISER_BASE_URL = os.environ.get(
    "R2E_VISUALISER_URL",
    "https://huggingface.co/spaces/HuggingFaceH4/harbor-visualiser",
)


@dataclass(slots=True)
class PushResult:
    repo_id: str
    commit_sha: str
    registry_url: str
    task_count: int


@dataclass(slots=True)
class PullResult:
    repo_id: str
    local_dir: Path
    task_count: int
    snapshot_path: Path  # actual on-disk location after snapshot_download


def _build_registry_json(
    repo_id: str,
    commit_sha: str,
    dataset_name: str,
    description: str,
    task_dirs: list[str],
) -> list[dict[str, Any]]:
    """Harbor's legacy registry.json format — list of DatasetSpec rows."""
    git_url = f"https://huggingface.co/datasets/{repo_id}"
    return [
        {
            "name": dataset_name,
            "version": "1.0",
            "description": description,
            "tasks": [
                {
                    "name": Path(task).name,
                    "git_url": git_url,
                    "git_commit_id": commit_sha,
                    "path": f"tasks/{Path(task).name}",
                }
                for task in task_dirs
            ],
        }
    ]


def _build_dataset_card(
    repo_id: str,
    dataset_name: str,
    task_count: int,
    repo_source: str,
    pipeline: str,
    visibility: str,
    source_repos: list[str] | None = None,
    has_environment: bool = False,
) -> str:
    """Render the HF Hub dataset card (README.md) for a published dataset.

    ``source_repos`` lets multi-repo datasets (e.g. a single ``pr_diff`` push
    that spans 25 repos) render a proper "Source repos" list; if absent, the
    single ``repo_source`` fallback is rendered. ``has_environment`` toggles
    the harbor-run section between a runnable-env recipe and the text-only
    fallback.
    """
    visualiser_link = f"{VISUALISER_BASE_URL}?dataset={repo_id}"

    if source_repos and len(source_repos) > 1:
        repo_lines = "\n".join(f"  - [`{r}`](https://github.com/{r})" for r in source_repos)
        source_block = f"- **Source repos** ({len(source_repos)}):\n{repo_lines}"
    elif repo_source:
        source_block = f"- **Source repo**: [`{repo_source}`](https://github.com/{repo_source})"
    else:
        source_block = "- **Source repo**: _(not stamped in metadata)_"

    if has_environment:
        run_section = f"""## Run with Harbor

Each task ships a `environment/Dockerfile` and `tests/test.sh`, so you can
score patches end-to-end:

```bash
# Pull the dataset locally
repo2rlenv pull {repo_id} /tmp/{dataset_name}

# Confirm structural soundness — oracle adapter applies the gold patch
# and must score reward = 1.000
harbor run -p /tmp/{dataset_name} -a oracle --env docker

# Score an agent (claude-code + Sonnet 4.6)
harbor run \\
  -p /tmp/{dataset_name} \\
  -a claude-code -m anthropic/claude-sonnet-4-6 \\
  --ak max_budget_usd=2.00 \\
  --ae ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \\
  --ve ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \\
  --env docker
```

The `--ve` (verifier env) pass is what enables the LLM-as-judge component
of the reward; without it the verifier still produces a valid 5-component
score, just with `llm_judge: null`."""
    else:
        run_section = f"""## Use with Harbor

```bash
harbor download {dataset_name} \\
  --registry-url https://huggingface.co/datasets/{repo_id}/resolve/main/registry.json
```"""

    return f"""---
license: apache-2.0
language:
  - en
size_categories:
  - n<1K
tags:
  - reinforcement-learning
  - code
  - llm
  - swe-rl
  - harbor
  - {pipeline}
---

[![View tasks in Harbor Visualiser](https://img.shields.io/badge/🤗%20Harbor%20Visualiser-View%20tasks-FFD21F?style=for-the-badge)]({visualiser_link})

# {dataset_name}

Generated by [**Repo2RLEnv**](https://github.com/huggingface/Repo2RLEnv) — turning real GitHub repositories into verifiable RL environments.

> 💡 **Browse this dataset in your browser** — click the badge above or open
> [`{VISUALISER_BASE_URL.removeprefix("https://huggingface.co/spaces/")}`]({visualiser_link})
> to inspect every task's spec, instruction, oracle patch, test script, and Dockerfile.

{source_block}
- **Pipeline**: [`{pipeline}`](https://github.com/huggingface/Repo2RLEnv/blob/main/docs/pipelines/{pipeline}.md)
- **Tasks**: {task_count}
- **Visibility**: {visibility}
- **Spec**: Harbor task format with the `[metadata.repo2env]` extension

## How it was generated

Each task in this dataset was produced by the [`{pipeline}` pipeline](https://github.com/huggingface/Repo2RLEnv/blob/main/docs/pipelines/{pipeline}.md). The pipeline mines real merged pull requests / commits from the source repo(s), applies quality filters, strips information-leakage from the instruction text, and emits a [Harbor](https://github.com/harbor-framework/harbor)-shaped task directory with the gold patch as the oracle.

Reproduce locally:

```bash
pip install repo2rlenv
repo2rlenv generate \\
  --repo <owner>/<repo> \\
  --pipeline {pipeline} \\
  --pipeline-opt limit=10 \\
  --out ./datasets/my-{pipeline}
```

See the [pipeline docs](https://github.com/huggingface/Repo2RLEnv/blob/main/docs/pipelines/{pipeline}.md) for the full option list + reward design.

{run_section}

## Reward signal

The reward function is part of the task itself (`tests/test.sh` + the
verifier code baked into the image). The full per-task breakdown is
written to `/logs/verifier/reward.json` at run time — useful for slicing
training data by component.

See the [pipeline doc]({f"https://github.com/huggingface/Repo2RLEnv/blob/main/docs/pipelines/{pipeline}.md"}#multi-component-reward) for the component-by-component design.

## Layout

```
tasks/
└── <task-id>/
    ├── task.toml          # Harbor task with [metadata.repo2env]
    ├── instruction.md     # natural-language prompt
    ├── solution/
    │   ├── patch.diff     # oracle (gold) diff
    │   └── solve.sh       # oracle adapter applies patch.diff
    ├── environment/
    │   └── Dockerfile     # builds the task's container
    └── tests/
        └── test.sh        # verifier — writes /logs/verifier/reward.txt
```

## License

Apache-2.0 — same as Repo2RLEnv itself. The original PR contents remain
under their respective source-repo licenses; this dataset redistributes
public commits under fair-use for ML research / training-data purposes.
"""


def _read_task_metadata(task_toml: Path) -> dict[str, Any]:
    """Extract `[metadata.repo2env]` from a task.toml. Empty dict on parse failure."""
    import tomllib

    try:
        data = tomllib.loads(task_toml.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.debug("could not parse %s: %s", task_toml, exc)
        return {}
    return data.get("metadata", {}).get("repo2env", {}) or {}


def push_to_hub(
    local_dataset_dir: Path,
    repo_id: str,
    auth: AuthSpec,
    *,
    private: bool = False,
    pipeline: str | None = None,
    repo_source: str | None = None,
    description: str = "",
    commit_message: str | None = None,
    # NEW (v0.8.2.post3): image-distribution controls
    image_registry: str | None = None,
    inline_dockerfile: bool = False,
    require_registry: bool = False,
    skip_image_push: bool = False,
    image_visibility: str = "inherit",
    on_message=None,
) -> PushResult:
    """Push a dataset directory to HF Hub. Returns the resulting registry URL.

    `pipeline` and `repo_source` are read out of the first task's
    `[metadata.repo2env]` subtable automatically. Callers can override
    (e.g. for legacy datasets missing that metadata) but normally don't
    need to — this keeps the dataset card accurate regardless of how
    `push_to_hub` is invoked.

    Image distribution (v0.8.2.post3): for _runtime datasets that ship an
    `environment/Dockerfile`, we discover a logged-in OCI registry, verify
    via probe (§5.3 of the plan), push the bootstrap image, and rewrite the
    task Dockerfile + task.toml to point at the registry digest. If no
    verified registry is available we fall back to inline-Dockerfile mode
    with a warning (unless `require_registry=True`).
    """
    from huggingface_hub import HfApi

    from repo2rlenv.registry.integration import prepare_dataset_for_push

    token = resolve_hf_token(auth)
    if not token:
        raise RuntimeError("no HF token resolved. Run `huggingface-cli login` or set HF_TOKEN.")

    api = HfApi(token=token)
    api.create_repo(repo_id, repo_type="dataset", private=private, exist_ok=True)

    # Image distribution: prepare the local dataset (in-place rewrite) BEFORE
    # we stage + upload. After this call, environment/Dockerfile + task.toml
    # are pointing at the canonical (registry-digest OR inline-recipe) form.
    hf_owner = repo_id.split("/", 1)[0]
    prepare = prepare_dataset_for_push(
        local_dataset_dir,
        hf_owner=hf_owner,
        image_registry=image_registry,
        inline_dockerfile=inline_dockerfile,
        require_registry=require_registry,
        skip_image_push=skip_image_push,
        image_visibility=image_visibility,  # type: ignore[arg-type]
        dataset_is_private=private,
        pushed_by=hf_owner,
        on_message=on_message,
    )
    logger.info(
        "image distribution: mode=%s tasks_rewritten=%d",
        prepare.mode,
        prepare.tasks_rewritten,
    )

    # Layout we expect locally: <root>/<task-id>/...
    # We'll re-stage it as <root>/tasks/<task-id>/...
    staging = local_dataset_dir / ".r2e_staging"
    staging.mkdir(exist_ok=True)
    tasks_dir = staging / "tasks"
    tasks_dir.mkdir(exist_ok=True)

    task_names = []
    first_metadata: dict[str, Any] = {}
    source_repos: set[str] = set()
    has_environment = False
    for child in sorted(local_dataset_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        toml_path = child / "task.toml"
        if not toml_path.exists():
            continue
        meta = _read_task_metadata(toml_path)
        if not first_metadata:
            first_metadata = meta
        if r := meta.get("repo"):
            source_repos.add(r)
        if (child / "environment" / "Dockerfile").exists():
            has_environment = True
        target = tasks_dir / child.name
        if target.exists():
            import shutil

            shutil.rmtree(target)
        import shutil as _sh

        _sh.copytree(child, target)
        task_names.append(child.name)

    if not task_names:
        raise RuntimeError(f"no Harbor tasks found in {local_dataset_dir}")

    # Pull authoritative values from the first task's [metadata.repo2env].
    # Caller-supplied overrides win (back-compat for legacy callers).
    if not pipeline:
        pipeline = first_metadata.get("pipeline", "pr_diff")
    if not repo_source:
        repo_source = first_metadata.get("repo", "")

    # Write README.md (dataset card)
    dataset_name = repo_id.split("/")[-1]
    (staging / "README.md").write_text(
        _build_dataset_card(
            repo_id=repo_id,
            dataset_name=dataset_name,
            task_count=len(task_names),
            repo_source=repo_source,
            pipeline=pipeline,
            visibility="private" if private else "public",
            source_repos=sorted(source_repos),
            has_environment=has_environment,
        ),
        encoding="utf-8",
    )

    # First upload — without registry.json (we need the commit SHA first)
    logger.info("uploading %d tasks to %s", len(task_names), repo_id)
    op = api.upload_folder(
        folder_path=str(staging),
        repo_id=repo_id,
        repo_type="dataset",
        commit_message=commit_message or f"Repo2RLEnv: add {len(task_names)} tasks",
    )
    commit_sha = (
        op.oid
        if hasattr(op, "oid")
        else api.list_repo_commits(repo_id, repo_type="dataset")[0].commit_id
    )

    # Now write registry.json that pins the commit SHA we just got
    registry = _build_registry_json(
        repo_id=repo_id,
        commit_sha=commit_sha,
        dataset_name=dataset_name,
        description=description or f"Repo2RLEnv {pipeline} from {repo_source}",
        task_dirs=task_names,
    )
    registry_path = staging / "registry.json"
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")

    api.upload_file(
        path_or_fileobj=str(registry_path),
        path_in_repo="registry.json",
        repo_id=repo_id,
        repo_type="dataset",
        commit_message="Add registry.json pinned to previous commit",
    )

    # Cleanup staging
    import shutil

    shutil.rmtree(staging)

    registry_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/registry.json"
    return PushResult(
        repo_id=repo_id,
        commit_sha=commit_sha,
        registry_url=registry_url,
        task_count=len(task_names),
    )


def pull_from_hub(
    repo_id: str,
    local_dir: Path,
    auth: AuthSpec | None = None,
    *,
    task: str | None = None,
    force: bool = False,
    revision: str | None = None,
) -> PullResult:
    """Pull a Repo2RLEnv-published dataset from HF Hub into a local directory.

    Reverses `push_to_hub`: the staged Hub layout has `tasks/<task-id>/...`,
    and we flatten that back to `<local_dir>/<task-id>/...` so the result is
    immediately consumable by `repo2rlenv validate` and `harbor run --path`.

    Args:
        repo_id: HF dataset id, e.g. "<your-org>/click-r2e".
        local_dir: where to materialize the flattened tasks.
        auth: optional AuthSpec; falls back to env-resolved HF token.
        task: if set, fetch only that single task subdir (faster, smaller).
        force: re-download even if a local snapshot already exists.
        revision: specific git ref on the Hub dataset (tag / branch / commit).
            None ⇒ default branch / latest commit.

    Returns a PullResult with the count of task directories materialized.
    """
    from huggingface_hub import snapshot_download

    auth = auth or AuthSpec()
    token = resolve_hf_token(auth)
    # token is optional for public datasets; pass-through if present

    if force and local_dir.exists():
        import shutil

        shutil.rmtree(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)

    allow_patterns = None
    if task:
        # Match the task subdir + the registry/README so a single-task pull
        # still produces a coherent dataset directory.
        allow_patterns = [
            f"tasks/{task}/**",
            "registry.json",
            "README.md",
        ]

    snapshot = Path(
        snapshot_download(
            repo_id=repo_id,
            repo_type="dataset",
            token=token,
            local_dir=str(local_dir / ".r2e_snapshot"),
            allow_patterns=allow_patterns,
            revision=revision,
        )
    )

    # Flatten staged `tasks/<id>/` → `<local_dir>/<id>/`
    staged_tasks = snapshot / "tasks"
    if not staged_tasks.exists():
        raise RuntimeError(
            f"{repo_id} doesn't look like a Repo2RLEnv dataset "
            f"(no `tasks/` subdir under {snapshot})"
        )

    import shutil

    task_count = 0
    for child in sorted(staged_tasks.iterdir()):
        if not child.is_dir():
            continue
        if not (child / "task.toml").exists():
            continue
        target = local_dir / child.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(child, target)
        task_count += 1

    # Also surface the registry.json + README.md at the dataset root so the
    # local layout matches what `push_to_hub` originally staged.
    for top in ("registry.json", "README.md"):
        src = snapshot / top
        if src.exists():
            shutil.copyfile(src, local_dir / top)

    # Clean up the snapshot scratch dir
    shutil.rmtree(snapshot, ignore_errors=True)

    if task_count == 0:
        raise RuntimeError(
            f"no Harbor tasks materialized from {repo_id} into {local_dir}"
            + (f" (filter: task={task!r})" if task else "")
        )

    return PullResult(
        repo_id=repo_id,
        local_dir=local_dir,
        task_count=task_count,
        snapshot_path=local_dir,
    )


def _flatten_to_task_layout(source: Path, local_dir: Path) -> int:
    """Materialize a directory of Harbor task dirs into `local_dir`.

    Accepts two layouts at `source`:
      - `<source>/tasks/<id>/task.toml` (HF-published / Harbor-published style)
      - `<source>/<id>/task.toml` (flat — common for git-cloned dataset repos)

    Copies each `<id>/` directory containing a `task.toml` into `local_dir`.
    Returns the number of tasks materialized.
    """
    import shutil

    candidates: list[Path] = []
    staged = source / "tasks"
    roots = [staged] if staged.is_dir() else [source]
    for root in roots:
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            if (child / "task.toml").exists():
                candidates.append(child)

    count = 0
    for child in candidates:
        target = local_dir / child.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(child, target, symlinks=False, ignore_dangling_symlinks=True)
        count += 1

    # Surface top-level registry.json + README.md if present
    for top in ("registry.json", "README.md"):
        src = source / top
        if src.exists():
            shutil.copyfile(src, local_dir / top)

    return count


def pull_from_harbor(
    name: str,
    local_dir: Path,
    *,
    tag: str | None = None,
    registry_url: str | None = None,
    force: bool = False,
) -> PullResult:
    """Pull a Harbor-registry dataset by shelling out to `harbor datasets download`.

    Harbor handles its own auth, caching, registry resolution, version pinning
    via `<name>@<tag>`. We just orchestrate the download then flatten the
    result into our standard local-dir layout.

    Args:
        name: Harbor dataset name (without `harbor://` prefix).
        local_dir: where to materialize.
        tag: optional version tag (`@lite`, `@verified`, ...). None = registry default.
        registry_url: optional custom Harbor registry URL.
        force: re-download even if local_dir already exists.
    """
    import shutil
    import subprocess
    import tempfile

    if not shutil.which("harbor"):
        raise RuntimeError(
            "`harbor` CLI not found on PATH. "
            "Install with `uv tool install harbor` (or `pip install harbor`), "
            "then re-run."
        )

    if force and local_dir.exists():
        shutil.rmtree(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)

    target = name + (f"@{tag}" if tag else "")

    with tempfile.TemporaryDirectory(prefix="r2e-harbor-pull-") as tmp:
        args = ["harbor", "datasets", "download", target, "-o", tmp]
        if registry_url:
            args += ["--registry-url", registry_url]
        logger.info("running: %s", " ".join(args))
        proc = subprocess.run(args, capture_output=True, text=True, timeout=600, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"harbor download failed (exit {proc.returncode}): "
                f"{proc.stderr.strip()[:400] or proc.stdout.strip()[:400]}"
            )

        # Harbor lays out tasks under tmp/. Walk and flatten.
        # The downloaded directory typically contains <name>/ or <name>/tasks/.
        downloaded = Path(tmp)
        # If there's exactly one subdirectory and it contains task.toml or tasks/,
        # recurse into it first; otherwise treat tmp as the dataset root.
        children = [c for c in downloaded.iterdir() if c.is_dir()]
        if len(children) == 1 and not (downloaded / "task.toml").exists():
            downloaded = children[0]

        task_count = _flatten_to_task_layout(downloaded, local_dir)

    if task_count == 0:
        raise RuntimeError(
            f"no Harbor tasks materialized for {target!r} into {local_dir}. "
            f"Either {name!r} isn't published, or the dataset uses a layout we don't recognize."
        )

    return PullResult(
        repo_id=target,
        local_dir=local_dir,
        task_count=task_count,
        snapshot_path=local_dir,
    )


def pull_from_github(
    owner_repo: str,
    local_dir: Path,
    *,
    ref: str | None = None,
    force: bool = False,
) -> PullResult:
    """Pull a dataset from a public/private GitHub repo via `git clone --depth 1`.

    The cloned repo must follow the Repo2RLEnv / Harbor task layout: either
    `<repo>/tasks/<id>/task.toml` (HF-style) or `<repo>/<id>/task.toml`
    (flat). We flatten to `<local_dir>/<id>/...` either way.

    Uses the same GitHub auth chain as `repo2rlenv generate` (gh CLI →
    $GITHUB_TOKEN → anonymous), so private repos work when you're `gh
    auth login`'d.

    Args:
        owner_repo: GitHub `owner/repo`.
        local_dir: where to materialize tasks.
        ref: optional branch / tag / commit SHA. None = default branch.
        force: re-clone even if local_dir already exists.
    """
    import shutil
    import subprocess
    import tempfile

    from repo2rlenv.auth import auth_clone_url, resolve_github_token
    from repo2rlenv.spec.input import AuthSpec, RepoSpec

    if not shutil.which("git"):
        raise RuntimeError("`git` not found on PATH; install git and retry.")

    parts = owner_repo.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"owner_repo must be 'owner/repo', got {owner_repo!r}")

    repo_spec = RepoSpec(url=f"https://github.com/{owner_repo}", access="auto")
    token = resolve_github_token(repo_spec, AuthSpec())
    clone_url = auth_clone_url(repo_spec.url, token)

    if force and local_dir.exists():
        shutil.rmtree(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="r2e-gh-pull-") as tmp:
        clone_dir = Path(tmp) / "clone"
        args = ["git", "clone", "--depth", "1"]
        if ref:
            args += ["--branch", ref]
        args += [clone_url, str(clone_dir)]
        logger.info("running: git clone --depth 1 [...] %s", owner_repo)
        proc = subprocess.run(args, capture_output=True, text=True, timeout=300, check=False)
        if proc.returncode != 0:
            stderr = proc.stderr.replace(token, "***") if token else proc.stderr
            raise RuntimeError(f"git clone failed (exit {proc.returncode}): {stderr.strip()[:400]}")

        task_count = _flatten_to_task_layout(clone_dir, local_dir)

    if task_count == 0:
        raise RuntimeError(
            f"no Harbor tasks materialized from gh://{owner_repo} into {local_dir}. "
            f"The repo doesn't appear to contain `task.toml` files in a recognized layout "
            f"(tried `tasks/<id>/` and `<id>/`)."
        )

    return PullResult(
        repo_id=f"gh://{owner_repo}" + (f"@{ref}" if ref else ""),
        local_dir=local_dir,
        task_count=task_count,
        snapshot_path=local_dir,
    )
