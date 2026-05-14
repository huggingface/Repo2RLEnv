# Python API reference

The CLI is a thin layer over the Python API. Anything the CLI does, you can do in code.

## Modules at a glance

| Module | Purpose |
|---|---|
| `repo2rlenv.spec` | Pydantic input/output models |
| `repo2rlenv.auth` | Token resolution (gh CLI, env vars) |
| `repo2rlenv.github` | Thin GitHub client wrapping `gh` CLI |
| `repo2rlenv.llm` | LiteLLM single-call wrapper |
| `repo2rlenv.reward` | Diff-similarity reward function |
| `repo2rlenv.config` | YAML/TOML config loader |
| `repo2rlenv.pipelines` | Pipeline implementations + registry |
| `repo2rlenv.emitter` | Task directory writers |
| `repo2rlenv.hub` | HF Hub push |
| `repo2rlenv.cli` | argparse entry points |

---

## `repo2rlenv.spec`

```python
from repo2rlenv.spec import (
    GenerationInput, RepoSpec, PipelineSpec, LLMSpec,
    OutputSpec, QASpec, SandboxSpec, AuthSpec,
    PipelineName, PRDiffOptions, PRRuntimeOptions,
)
from repo2rlenv.spec.options import parse_options, OPTIONS_REGISTRY
```

Build a `GenerationInput` directly:

```python
g = GenerationInput(
    repo=RepoSpec(url="huggingface/trl", access="auto"),
    pipeline=PipelineSpec(name=PipelineName.PR_DIFF, options={"limit": 5}),
    llm=LLMSpec(provider="anthropic", model="claude-sonnet-4-6"),
    output=OutputSpec(
        destination="./out", org="myorg", dataset_name="trl-r2e",
    ),
)
```

`PipelineSpec.options` is a free dict at the spec level; the dispatcher validates it strictly via `parse_options(name, dict)` against the named pipeline's `Options` model.

## `repo2rlenv.auth`

```python
from repo2rlenv.auth import (
    resolve_github_token,
    resolve_hf_token,
    resolve_llm_api_key,
    auth_clone_url,
)
```

`resolve_github_token(repo, auth) -> str | None` — implements the four-step resolution chain documented in [AUTH.md](./AUTH.md). Returns `None` if anonymous.

`auth_clone_url(url, token) -> str` — injects a token into a clone URL using GitHub's `x-access-token` form. Pass-through if `token` is `None`.

## `repo2rlenv.github`

```python
from repo2rlenv.github import list_merged_prs, fetch_pr_diff, PullRequestSummary
```

`list_merged_prs(owner, name, *, limit, since, until, skip_drafts, token)` — paginates `gh pr list` and returns `PullRequestSummary` objects with PR title, body, base SHA, head SHA, URL, and changed files.

`fetch_pr_diff(owner, name, number, *, token)` — returns the unified diff as a string via `gh pr diff`.

## `repo2rlenv.llm`

```python
from repo2rlenv.llm import complete

response = complete(
    spec,                         # LLMSpec
    system="...",                 # optional
    user="...",
    max_tokens=1024,
    temperature=0.7,
)
print(response.content)
```

Single-shot LiteLLM call. Honors `spec.endpoint` for self-hosted backends; auto-points HF provider at `https://router.huggingface.co/v1`.

## `repo2rlenv.reward`

```python
from repo2rlenv.reward import calculate_diff_similarity_reward

reward, meta = calculate_diff_similarity_reward(oracle_diff, predicted_diff)
# reward ∈ [0, 1]; identical-after-normalization ⇒ 1.0; empty pred ⇒ 0.0
```

Pure stdlib (`difflib.SequenceMatcher`). Normalizes volatile metadata (hunk headers, index lines) before comparing.

## `repo2rlenv.pipelines`

```python
from repo2rlenv.pipelines import PIPELINES, Pipeline, PipelineResult
from repo2rlenv.pipelines.pr_diff import PRDiffPipeline

cls = PIPELINES["pr_diff"]
pipeline = cls(generation_input, options)
result = pipeline.run(out_dir)   # returns PipelineResult(candidates, emitted, skipped, out_dir, skip_reasons)
```

`Pipeline` is a `runtime_checkable` Protocol — every entry in `PIPELINES` duck-conforms. `PipelineResult` is the standard return shape across pipelines. See [pipelines/](./pipelines/) for per-pipeline docs.

## `repo2rlenv.emitter.harbor`

```python
from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task

task = HarborTask(
    name="repo__name-1",
    org="myorg",
    description="...",
    instruction="...",
    oracle_diff="...",
    repo2env={"pipeline": "pr_diff", ...},
)
path = write_harbor_task(task, dest_dir)
```

Writes `task.toml`, `instruction.md`, `solution/patch.diff`. The `[metadata.repo2env]` subtable is auto-completed with `spec_version`, `content_hash`, and `reward_kinds`.

## `repo2rlenv.hub`

```python
from repo2rlenv.hub import push_to_hub

result = push_to_hub(
    local_dataset_dir=Path("./out"),
    repo_id="AdithyaSK/trl-r2e-v0-1",
    auth=auth_spec,
    private=False,
    pipeline="pr_diff",
    repo_source="huggingface/trl",
)
print(result.registry_url)
```

Two-commit upload: tasks first, then `registry.json` pinned to the resulting commit SHA. Uses `huggingface_hub.HfApi.upload_folder`.

## Running tasks

Repo2RLEnv ships **no execution runtime**. To run/score:

- **Diff-similarity scoring** — call `reward.calculate_diff_similarity_reward(oracle, prediction)` directly from Python. Used by RL training loops where running tests every rollout is too expensive. There is no CLI wrapper.
- **Test execution** — use `harbor run --agent <agent> --path <task>`. Repo2RLEnv emits Harbor-compatible task directories that work out of the box across Harbor's Local Docker / Modal / Daytona / E2B / Runloop backends.

## CLI ↔ API mapping

| CLI subcommand | Python equivalent |
|---|---|
| `repo2rlenv generate ...` | `pipelines.PIPELINES[name](input, opts).run(out_dir)` |
| `repo2rlenv validate <path>` | walk task.toml files + `tomllib.loads` |
| `repo2rlenv push <dir> <hf://...>` | `hub.push_to_hub(local_dir, repo_id, auth, ...)` |
| `repo2rlenv pull <hf://...> [<dir>]` | `hub.pull_from_hub(repo_id, local_dir, auth, ...)` |
| `repo2rlenv bootstrap ...` | `bootstrap.ensure_bootstrap(repo, spec, llm)` |
| diff-similarity reward | `reward.calculate_diff_similarity_reward(oracle, prediction)` (Python only) |
| test-execution reward | `harbor run --agent <agent> --path <task>` (separate tool) |
