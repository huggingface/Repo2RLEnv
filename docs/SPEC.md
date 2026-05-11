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
| `OutputSpec` | `destination`, `org`, `dataset_name` | `destination` may be a local path or `hf://owner/name` |
| `QASpec` | (none) | Defaults to `[diff_parse]` for the lite path; full pipelines opt into `[determinism, oracle_consistency, llm_judge, false_negative]` |
| `SandboxSpec` | (none) | See "Sandbox model" below — `none` for lite, `harbor` for full pipelines (delegates), `local`/`e2b` for lite consumer-side runners |
| `AuthSpec` | (none) | Names of env vars only — values never stored |

### Two equivalent invocation forms

**Flag form**:

```bash
repo2rlenv generate \
  --repo huggingface/trl \
  --pipeline pr_mining_lite \
  --pipeline-opt limit=5 \
  --llm anthropic/claude-sonnet-4-6 \
  --out hf://AdithyaSK/trl-r2e-v0-1 \
  --org AdithyaSK --dataset-name trl-r2e-v0-1
```

**Config-file form** — `--config <path>` accepts YAML or TOML, format auto-detected by extension. CLI flags override file fields:

```yaml
spec_version: "0.1.0"
repo:
  url: "huggingface/trl"
  access: "auto"
pipeline:
  name: "pr_mining_lite"
  options:
    limit: 5
    skip_drafts: true
llm:
  provider: "anthropic"
  model: "claude-sonnet-4-6"
output:
  destination: "hf://AdithyaSK/trl-r2e-v0-1"
  org: "AdithyaSK"
  dataset_name: "trl-r2e-v0-1"
  visibility: "public"
```

`repo2rlenv init` writes a sample config you can tweak.

## Output contract — Harbor + `[metadata.repo2env]`

Every pipeline emits **standard Harbor task directories**. Repo2RLEnv-specific provenance goes into a namespaced subtable inside Harbor's existing `[metadata]`.

### Task directory layout

```
<dataset>/<task_id>/
├── task.toml                # Harbor-native + [metadata.repo2env]
├── instruction.md           # natural-language prompt
├── solution/patch.diff      # oracle (lite) — for diff_similarity scoring
├── environment/Dockerfile   # OPTIONAL — only emitted by sandbox-required pipelines
└── tests/test.sh            # OPTIONAL — only emitted by sandbox-required pipelines
```

For the **lite** pipeline (`pr_mining_lite`), only the first three exist. No Docker, no test script — verification is purely diff-similarity against the oracle.

### `task.toml` example

```toml
version = "1.0"

[task]
name = "huggingface__trl-5705"
org = "AdithyaSK"
description = "..."

[metadata]
difficulty = "medium"
category = "bugfix"

[metadata.repo2env]
spec_version = "0.1.0"
pipeline = "pr_mining_lite"
pipeline_version = "0.1.0"
repo = "huggingface/trl"
ref = "f39373edcd7a..."           # base commit SHA
reference = "https://github.com/huggingface/trl/pull/5705"
source_access = "public"
built_at = "2026-05-06T..."
synthesis_llm = "anthropic/claude-sonnet-4-6"
content_hash = "sha256:..."
reward_kinds = ["diff_similarity"]

[metadata.repo2env.pr_mining_lite]
pr_merged_at = "2026-05-05T13:46:07Z"
diff_format = "unified"
context_files = ["trl/trainer/dpo_trainer.py", ...]

[agent]
timeout_sec = 1800.0

[verifier]
timeout_sec = 300.0
```

Each pipeline writes its own subtable under `[metadata.repo2env.<name>]` carrying provenance specific to how the task was made — see the per-pipeline docs for the schema.

### Dataset-level layout (HF Hub)

When pushed via `repo2rlenv push` or `--out hf://...`, the dataset on the Hub looks like:

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
| Generation | Lite (text-only, e.g. `pr_mining_lite`) | Nothing — pure text manipulation |
| Generation | Full (`pr_mining`, `mutation`, etc.) | Harbor's sandbox layer (`harbor` invoked under the hood) |
| Consumption | Lite | Just call `repo2rlenv reward` — no sandbox needed |
| Consumption | Full | `harbor run -d <dataset> -e <modal\|daytona\|e2b\|local\|runloop> ...` |

`SandboxSpec` exists to *describe* what the pipeline needs (provider, GPU, network), and at gen-time we lower it onto Harbor's flags. We don't ship a parallel runner. This keeps the surface area small — Harbor already handles GPU, multi-container, parallelism, and provider auth.

### GPU

```python
class GPUSpec(BaseModel):
    count: int = 1
    kind: Literal["any", "a10g", "a100", "h100", "l4", "t4"] = "any"
```

GPU is only meaningful for **sandbox-required pipelines on ML repos** — e.g., mining `huggingface/trl` with full `pr_mining` will skip most interesting PRs unless the verifier sandbox has a GPU because the trainer tests require CUDA.

Lite pipelines never use this field. When set on a `harbor`-provider sandbox, we pass it through to the Harbor backend's GPU config (Modal A100 / H100 / etc.).

## Reward kinds

`[metadata.repo2env.reward_kinds]` is a list naming the reward types this task supports. Two are defined for v0.1:

| Kind | What it is | Where the oracle lives |
|---|---|---|
| `diff_similarity` | SWE-RL-style sequence similarity between predicted and oracle unified diffs (returns float ∈ [0,1]) | `solution/patch.diff` |
| `test_execution` | Shell verifier writes a float to `/logs/verifier/reward.txt` | `tests/test.sh` |

A task may emit both. The lite pipeline emits only `diff_similarity`; full sandbox-required pipelines emit `test_execution` (and may also emit `diff_similarity` if they capture the oracle as a diff).

The diff-similarity reward function is implemented at [`src/repo2rlenv/reward.py:calculate_diff_similarity_reward`](../src/repo2rlenv/reward.py) — pure stdlib (`difflib.SequenceMatcher`), Apache-2.0, no SWE-RL CC-BY-NC code vendored.

## Conformance

A task or dataset is **conformant** to v0.1 if and only if:

1. `repo2rlenv validate <path>` exits 0
2. `task.toml` is valid TOML and contains `[task].name`
3. The named `[metadata.repo2env.pipeline]` matches a registered pipeline
4. `solution/patch.diff` exists and is non-empty (lite pipelines)
5. For sandbox-required pipelines: `environment/Dockerfile` and `tests/test.sh` exist

## Versioning

Pre-1.0 is a moving target — minor bumps may break readers. After 1.0 we honor strict SemVer (additive minors, breaking majors only). Each released spec version freezes its JSON Schema at a stable URL.
