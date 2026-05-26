# Input / Output Spec

Two contracts: what you feed Repo2RLEnv (input) and what comes out (output). Same input shape across every pipeline; pipeline-specific knobs go under `pipeline.options` (the "kwargs"). Output is **pure Harbor** with a namespaced `[metadata.repo2env]` extension.

## Input contract — `GenerationInput`

The single root model is [`src/repo2rlenv/spec/input.py:GenerationInput`](../src/repo2rlenv/spec/input.py). The CLI is a thin shim that builds this object from flags and/or a YAML/TOML config file.

```python
class GenerationInput(BaseModel):
    spec_version: Literal["0.1.0"] = "0.1.0"
    repo: RepoSpec                       # source repository
    pipeline: PipelineSpec               # synthesis method + kwargs
    llm: LLMSpec                         # model driving synthesis
    output: OutputSpec                   # where the dataset lands
    qa: QASpec = QASpec()                # quality gate (default: diff_parse only for lite)
    sandbox: SandboxSpec = SandboxSpec() # execution backend (default: none for lite)
    auth: AuthSpec = AuthSpec()          # secret references, resolved from env
```

### Sub-models

| Model | Required fields | Notes |
|---|---|---|
| `RepoSpec` | `url` | `access ∈ {public, private, auto}`, optional `auth_token_env`, `ref` defaults to `HEAD` |
| `PipelineSpec` | `name`, `options` | `name` is an enum (see [pipelines/](./pipelines/)); `options` is validated against the named pipeline's Options model with `extra="forbid"` |
| `LLMSpec` | `provider`, `model` | `provider/model` resolves to a LiteLLM identifier; supports `endpoint` for self-hosted vLLM/Ollama |
| `OutputSpec` | `destination`, `org`, `dataset_name` | `destination` is a local path; publish separately via `repo2rlenv push` |
| `QASpec` | (none) | Defaults to `[diff_parse]` for the lite path; full pipelines opt into `[determinism, oracle_consistency, llm_judge, false_negative]` |
| `SandboxSpec` | (none) | See "Sandbox model" below — `none` for lite, `harbor` for full pipelines (delegates), `local`/`e2b` for lite consumer-side runners |
| `AuthSpec` | (none) | Names of env vars only — values never stored |

### Two equivalent invocation forms

**Flag form**:

```bash
# Generate locally, then push as two explicit steps
repo2rlenv generate \
  --repo huggingface/trl \
  --pipeline pr_diff \
  --pipeline-opt limit=5 \
  --llm anthropic/claude-sonnet-4-6 \
  --out ./datasets/trl-r2e-v0-1

repo2rlenv push ./datasets/trl-r2e-v0-1 <your-org>/trl-r2e-v0-1
```

**Config-file form** — `--config <path>` accepts YAML or TOML, format auto-detected by extension. CLI flags override file fields:

```yaml
spec_version: "0.1.0"
repo:
  url: "huggingface/trl"
  access: "auto"
pipeline:
  name: "pr_diff"
  options:
    limit: 5
    skip_drafts: true
llm:
  provider: "anthropic"
  model: "claude-sonnet-4-6"
output:
  destination: "./datasets/trl-r2e-v0-1"
  org: "<your-org>"
  dataset_name: "trl-r2e-v0-1"
  visibility: "public"
```

Drop this into a file (e.g. `repo2rlenv.config.yaml`) and run with `--config <path>`. Publishing is a separate step via `repo2rlenv push`.

## Output contract — Harbor + `[metadata.repo2env]`

Every pipeline emits **standard Harbor task directories**. Repo2RLEnv-specific provenance goes into a namespaced subtable inside Harbor's existing `[metadata]`.

### Task directory layout

```
<dataset>/<task_id>/
├── task.toml                # Harbor-native + [metadata.repo2env]
├── instruction.md           # natural-language prompt
├── solution/patch.diff      # oracle (lite) — for diff_similarity scoring
├── environment/Dockerfile   # OPTIONAL — emitted by sandbox-required pipelines AND by pr_diff (emit_harbor_env=True)
└── tests/test.sh            # OPTIONAL — emitted alongside environment/Dockerfile
```

`pr_diff` generates without a sandbox, but by default (`emit_harbor_env=True`) it still emits an `environment/Dockerfile` (thin `python:3.12-slim`) + a `tests/test.sh` carrying its 6-component diff-similarity verifier, so the task is runnable directly via `harbor run`. With `emit_harbor_env=False` only the first three files exist (pure text — the consumer scores the diff themselves).

### `task.toml` example

```toml
version = "1.0"

[task]
name = "huggingface__trl-5705"
org = "<your-org>"
description = "..."

[metadata]
difficulty = "medium"
category = "bugfix"

[metadata.repo2env]
spec_version = "0.2.0"
pipeline = "pr_diff"
pipeline_version = "0.1.0"
repo = "huggingface/trl"
ref = "f39373edcd7a..."           # base commit SHA
reference = "https://github.com/huggingface/trl/pull/5705"
source_access = "public"
built_at = "2026-05-06T..."
synthesis_llm = "anthropic/claude-sonnet-4-6"
content_hash = "sha256:..."
reward_kinds = ["diff_similarity"]

[metadata.repo2env.pr_diff]
pr_merged_at = "2026-05-05T13:46:07Z"
diff_format = "unified"
context_files = ["trl/trainer/dpo_trainer.py", ...]

# v0.2.0+ only — sandbox-required tasks (pr_runtime / mutation_bugs / ...)
# carry this subtable so consumers know exactly what they're getting.
[metadata.repo2env.reproducibility]
mode = "registry"                                    # registry | inline_dockerfile | local_only
image_ref = "ghcr.io/huggingface/r2e-bootstrap-pallets-click@sha256:..."
image_tag = "ghcr.io/huggingface/r2e-bootstrap-pallets-click:a1b2c3d4e5f6-7d8e9f01"
image_visibility = "public"                          # public | private | unknown
pushed_at = "2026-05-19T11:30:00Z"
pushed_by = "huggingface"
# Inline-mode-only fields (omitted in registry mode):
# inline_recipe_sha256 = "sha256:..."
# inline_recipe_lines = 47
# inline_recipe_source = "agent_replay"              # or "user_dockerfile"
# fallback_reason = "no working registry credentials (ghcr.io: L2 auth failed)"

[agent]
timeout_sec = 1800.0

[verifier]
timeout_sec = 300.0
```

Each pipeline writes its own subtable under `[metadata.repo2env.<name>]` carrying provenance specific to how the task was made — see the per-pipeline docs for the schema.

### Dataset-level layout (HF Hub)

When pushed via `repo2rlenv push`, the dataset on the Hub looks like:

```
huggingface.co/datasets/<owner>/<name>/
├── README.md                # auto-generated dataset card
├── registry.json            # Harbor's legacy registry format, pinned to a commit SHA
└── tasks/
    └── <task_id>/...
```

`registry.json` lets any Harbor consumer pull tasks directly:

```bash
harbor download <dataset-name> \
  --registry-url https://huggingface.co/datasets/<owner>/<name>/resolve/main/registry.json
```

Implementation: [`src/repo2rlenv/hub.py:push_to_hub`](../src/repo2rlenv/hub.py).

## Sandbox model — we don't have one

Repo2RLEnv has **no sandbox abstraction of its own**. Generation-time execution and consumption-time execution both go through external tools:

| Phase | Pipeline class | What runs the code |
|---|---|---|
| Generation | Lite (text-only generation, e.g. `pr_diff`) | Nothing — pure text manipulation (no sandbox at gen time) |
| Generation | Full (`pr_runtime`, `mutation_bugs`, etc.) | Harbor's sandbox layer (`harbor` invoked under the hood) |
| Consumption | `pr_diff` (default `emit_harbor_env=True`) | `harbor run` against the thin `python:3.12-slim` env + baked 6-component verifier |
| Consumption | `pr_diff` (`emit_harbor_env=False`) or any stored-diff task | `from repo2rlenv.reward import calculate_diff_similarity_reward` — pure Python, no sandbox |
| Consumption | Full | `harbor run -d <dataset> -e <modal\|daytona\|e2b\|local\|runloop> ...` |

`SandboxSpec` exists to *describe* what the pipeline needs (provider, GPU, network), and at gen-time we lower it onto Harbor's flags. We don't ship a parallel runner. This keeps the surface area small — Harbor already handles GPU, multi-container, parallelism, and provider auth.

### GPU

```python
class GPUSpec(BaseModel):
    count: int = 1
    kind: Literal["any", "a10g", "a100", "h100", "l4", "t4"] = "any"
```

GPU is only meaningful for **sandbox-required pipelines on ML repos** — e.g., mining `huggingface/trl` with full `pr_runtime` will skip most interesting PRs unless the verifier sandbox has a GPU because the trainer tests require CUDA.

Lite pipelines never use this field. When set on a `harbor`-provider sandbox, we pass it through to the Harbor backend's GPU config (Modal A100 / H100 / etc.).

## Reward kinds

`[metadata.repo2env.reward_kinds]` is a list naming the reward types this task supports. Two are defined for v0.1:

| Kind | What it is | Where the oracle lives |
|---|---|---|
| `diff_similarity` | Similarity between the predicted and oracle unified diffs (float ∈ [0,1]). `pr_diff` scores this with a 6-component verifier (format / size / file-targeting / region-overlap / changes-only similarity / LLM-judge) written to `/logs/verifier/reward.txt`; the simpler stdlib `SequenceMatcher` path is available for `emit_harbor_env=False` tasks. | `solution/patch.diff` |
| `test_execution` | Shell verifier writes a float to `/logs/verifier/reward.txt` | `tests/test.sh` |

A task may emit both. The lite pipeline emits only `diff_similarity`; full sandbox-required pipelines emit `test_execution` (and may also emit `diff_similarity` if they capture the oracle as a diff).

The diff-similarity reward function is implemented at [`src/repo2rlenv/reward.py:calculate_diff_similarity_reward`](../src/repo2rlenv/reward.py) — pure stdlib (`difflib.SequenceMatcher`), Apache-2.0, no SWE-RL CC-BY-NC code vendored.

## Image distribution (v0.2.0+)

Sandbox-required tasks (`pr_runtime`, `mutation_bugs`, …) ship an `environment/Dockerfile` whose `FROM <ref>` line points at the *bootstrap image* — the working Docker environment for the source repo. At generate time the ref is `local/r2e-bootstrap/...` (un-pullable from any other machine). `repo2rlenv push` rewrites it in-place to one of two reproducible forms:

| Mode | `FROM` ref | Reproducibility |
|---|---|---|
| `registry` | `ghcr.io/<owner>/r2e-bootstrap-<slug>@sha256:...` (or ECR / ACR / GCP AR / Docker Hub equivalent) | **Bit-exact** — registry digest is immutable |
| `inline_dockerfile` | full apt-get / pip / ... recipe baked into `environment/Dockerfile`, no `FROM <registry>` reference | **Recipe-level** — assumes mirrors stay stable; rebuilds from scratch on every `harbor run` |

The mode that was chosen is recorded in `[metadata.repo2env.reproducibility]` (see the `task.toml` example above), along with `pushed_at`, `pushed_by`, and — for inline mode — `inline_recipe_source` ∈ `{user_dockerfile, agent_replay}`.

`repo2rlenv push` decides which mode to use by running the **OCI Distribution Spec L1–L4 probe protocol** against every registry it finds credentials for in `~/.docker/config.json`. The probe never pushes anything to the user's registry — it confirms reachability + auth + read + write by starting a blob-upload session and immediately cancelling it. Run `repo2rlenv push --check-auth` to see the probe output for your machine.

Push flags:

| Flag | Behaviour |
|---|---|
| (default) | Auto-detect a verified registry; fall back to inline-Dockerfile mode with a warning if none |
| `--image-registry <prefix>` | Force a specific registry (e.g. `ghcr.io/myorg`); probed for write access before push |
| `--inline-dockerfile` | Skip image push; bake the recipe into each task. Recipe-level reproducibility. |
| `--require-registry` | Hard-fail if no verified registry is available (CI / launch mode). No silent fallback. |
| `--skip-image-push` | Rewrite tasks against a remote ref that already exists at the registry. No `docker push`. |
| `--image-visibility public\|private\|inherit` | Visibility for the pushed image (GHCR auto-flips via the GitHub API). Default: match dataset. |
| `--check-auth` | Probe every detected registry and exit. `--fast` skips L3/L4; `--json` for CI. |

## Conformance

A task or dataset is **conformant** to v0.1 if and only if:

1. `repo2rlenv validate <path>` exits 0
2. `task.toml` is valid TOML and contains `[task].name`
3. The named `[metadata.repo2env.pipeline]` matches a registered pipeline
4. `solution/patch.diff` exists and is non-empty (lite pipelines)
5. For sandbox-required pipelines: `environment/Dockerfile` and `tests/test.sh` exist

v0.2 adds:

6. Sandbox-required tasks carry `[metadata.repo2env.reproducibility]` with `mode ∈ {registry, inline_dockerfile, local_only}`. `local_only` is pre-publication — these tasks are NOT considered reproducible by external consumers.

## Versioning

Pre-1.0 is a moving target — minor bumps may break readers. After 1.0 we honor strict SemVer (additive minors, breaking majors only). Each released spec version freezes its JSON Schema at a stable URL.

### v0.2.0 (v0.8.2.post3)

Adds `[metadata.repo2env.reproducibility]`. Additive change — v0.1.0 readers ignore the new subtable; pre-v0.2 datasets that lack it pass validation unchanged but aren't portable across machines.
