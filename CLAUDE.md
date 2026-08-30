# CLAUDE.md — project memory for Repo2RLEnv

Auto-loaded by Claude Code for anyone working in this repo. It is **committed and
shared**, so it holds only what is true for any clone: architecture, invariants,
and the mistakes that are easy to make here.

It is not a changelog and not a contributor guide. Those live elsewhere:

| You want | Read |
|---|---|
| Dev setup, PR/commit conventions, code style, releases | [`CONTRIBUTING.md`](./CONTRIBUTING.md) |
| Install → first dataset → push | [`docs/quickstart.md`](./docs/quickstart.md) |
| What each pipeline does, its yield and options | [`docs/pipelines/README.md`](./docs/pipelines/README.md) |
| Why a pipeline exists in its shape | [`docs/rfcs/`](./docs/rfcs/README.md) |
| Version history | [`docs/release_notes/HISTORY.md`](./docs/release_notes/HISTORY.md) |

If anything here conflicts with the code, **trust the code** and fix this file.

## What this is

**Repo2RLEnv** (`repo2rlenv` on PyPI) turns a repository into a verifiable RL
training/eval dataset. End-to-end: **synthesis → standardize → train + eval**,
focused on training. Datasets are emitted in the
[Harbor](https://github.com/harbor-framework/harbor) task format so they drop
straight into Harbor's runtime ecosystem (Local Docker / Modal / Daytona / E2B /
Runloop + 22 agent harnesses).

GitHub: https://github.com/huggingface/Repo2RLEnv · PyPI: `repo2rlenv` · Apache-2.0.

Three layers, only the first is ours:

| Layer | We ship | We delegate |
|---|---|---|
| **Generation** (pipelines that produce tasks) | `src/repo2rlenv/pipelines/` — the moat | — |
| **Spec** (uniform output format) | `[metadata.repo2env]` extension to Harbor's `task.toml` | Harbor's task spec |
| **Consumption** (sandboxes / agents / runtime) | HF Hub publish bridge; planned TRL trainer bridge | Harbor's full stack |

## Invariants

Break these and the build, the CI, or the science breaks:

1. **Never `print()` in library or CLI code.** Everything goes through
   `repo2rlenv.ui.console` — see [UI conventions](#ui-conventions).
2. **Never hand-edit `pyproject.toml`'s `dependencies`.** Use `uv add <pkg>` /
   `uv add --dev <pkg>`, or it desyncs from `uv.lock`.
3. **Run `uv run pytest tests/test_pipeline_contract.py` after touching
   `pipelines/`.** It fails for any registered pipeline that breaks the Protocol.
4. **Anti-contamination is enforced by the environment, never requested in the
   prompt.** Every emitted task goes through `pipelines/_env_guard.py`. Don't add
   "please don't look up the fix" to an instruction — scrub the history and cut
   the egress instead.
5. **New pipeline ⇒ RFC first.** Any new `PipelineName` entry needs a
   `docs/rfcs/NNNN-*.md` before the implementation PR.
6. **New docs page ⇒ add it to `mkdocs.yml`'s `nav:`**, or it won't ship on the site.
7. **`from __future__ import annotations` at the top of every module.**
8. **Version bumps never ride along in a feature PR** — they happen on `main`
   after merge, as their own commit.
9. **Don't commit generated artifacts.** Datasets go to the HF Hub; local run
   output stays gitignored.

## Pipelines

Registry: `pipelines/__init__.py:PIPELINES`. Names: `spec/input.py:PipelineName`.
Per-pipeline detail (yield, sources, options, reference datasets):
[`docs/pipelines/README.md`](./docs/pipelines/README.md).

| Pipeline | Shape | Status | RFC |
|---|---|---|---|
| `pr_diff` | text-only generation; thin Docker env for the 6-component diff-similarity reward | stable | 0001 |
| `pr_runtime` | mines PRs, verifies an F2P/P2P oracle in the bootstrap sandbox | stable | 0002 |
| `commit_runtime` | same, at commit level (bypasses PR-review filters) | stable | 0003 |
| `code_instruct` | LLM authors a problem + verifier anchored to real repo source | experimental | 0004 |
| `equivalence_tests` | extract a pure function; LLM writes tests against a `reference_<name>` oracle | experimental | 0005 |
| `cve_patches` | OSV CVE → fix commit → task; reuses the `pr_runtime` verifier | experimental | 0006 |

RFCs **0007 `pr_to_env` / 0008 `env_setup` / 0009 `test_synthesis` /
0010 `issue_runtime`** are drafted but not built.

Names follow `{source}_{shape}`:

- `_diff` — no sandbox at *generation* time. (`pr_diff` still emits a Docker env so
  its reward runs under `harbor`; "no sandbox" refers to generation, not consumption.)
- `_runtime` — runs inside the bootstrap sandbox to verify the oracle.
- `_patches` / `_instruct` / `_tests` — the artifact type, for synthesized pipelines.
- `_to_env` — import-shape: the caller supplies candidates, the pipeline doesn't
  mine them. Proposed in RFC 0007; no shipped pipeline uses it yet.

`_bugs` and `_synthesis` were retired with `mutation_bugs` / `refactor_synthesis`.

### The contract

```python
class Pipeline(Protocol):
    name: ClassVar[PipelineName]
    def __init__(self, input: GenerationInput, options: BaseModel) -> None: ...
    def run(self, out_dir: Path) -> PipelineResult: ...
```

Walkthrough for adding one:
[`docs/contributing/ADDING_A_PIPELINE.md`](./docs/contributing/ADDING_A_PIPELINE.md).

## Repo map

```
src/repo2rlenv/
├── cli.py                      # argparse; subcommands: generate validate push pull bootstrap
├── spec/
│   ├── input.py                # GenerationInput, RepoSpec, PipelineName, LLMSpec, OutputSpec
│   └── options.py              # per-pipeline options models (PRDiffOptions, …)
│                               # NB: the OUTPUT model is emitter/harbor.py:HarborTask
├── pipelines/                  # `_`-prefixed files are SHARED MACHINERY, not pipelines
│   ├── base.py                 # Pipeline Protocol + PipelineResult
│   ├── pr_diff.py · pr_runtime.py · commit_runtime.py
│   ├── code_instruct.py · equivalence_tests.py · cve_patches.py
│   ├── pr_runtime_validate.py  # shared PR validation harness (pr_runtime, cve_patches)
│   ├── _env_guard.py           # anti-contamination: git-history scrub + egress guard (EVERY task)
│   ├── _eval_script.py         # verifier-script + diff helpers (make_unified_diff, all_tests_passed)
│   ├── _pr_diff_verifier.py    # in-container 6-component diff-similarity reward (stdlib, base64-baked)
│   ├── _pr_runtime_verifier.py # in-container graded F2P/P2P reward
│   ├── _oss_instruct.py        # code_instruct synthesis + its 4 quality gates
│   ├── _function_extractor.py  # equivalence_tests: purity/self-containment filter, AST rename
│   └── _poc_agent.py           # cve_patches: LLM+shell PoC-test synthesis in the vuln sandbox
├── bootstrap/                  # LLM-driven Docker env generation
│   ├── runner.py               # ensure_bootstrap() orchestrator
│   ├── agent.py                # ReAct loop (enforces max_llm_spend_usd)
│   ├── docker.py               # DockerSandbox primitives
│   ├── language.py             # auto-detect Python/JS/Go/Rust/...
│   ├── cache.py                # content-addressed cache, keyed on bootstrap opts
│   └── spec.py                 # BootstrapSpec / BootstrapResult
├── registry/                   # bootstrap-image distribution
│   ├── push.py · auth.py · naming.py · probe.py · visibility.py · integration.py
│   └── ecr.py · gar.py         # AWS ECR + Google AR, beyond GHCR / Docker Hub
├── log_parsers/                # test output → F2P/P2P sets
│   └── pytest_parser.py · go_parser.py · cargo_parser.py · jest_parser.py
├── ui/                         # Rich UI — every CLI surface goes through here
│   ├── console.py              # singleton R2EConsole + install_logging()
│   ├── theme.py · primitives.py · live.py
│   └── views/{bootstrap,generation}.py
├── emitter/harbor.py           # Task → Harbor task.toml directory writer
├── sources.py · provider.py    # source kinds + Capability gating; provider dispatch
├── github.py · gitlab.py · git_local.py · osv.py    # the four input backends
├── hub.py                      # HF Hub push/pull + Harbor-compatible registry.json
├── auth.py                     # token resolution (repo / LLM / registry / Hub)
├── llm.py                      # LiteLLM wrapper + completion_cost tracking
├── reward.py                   # SWE-RL-style diff-similarity reward (stdlib only)
└── config.py                   # YAML/TOML config loader

tests/                  # unit tests mirror the module they cover; e2e in test_e2e_*.py
docs/                   # mkdocs site — nav lives in mkdocs.yml
├── index.md            #   site home (there is no docs/README.md)
├── quickstart.md
├── pipelines/          #   README.md + one page per pipeline
├── rfcs/               #   0001–0010 + TEMPLATE.md + process README
├── reference/          #   SPEC · API · AUTH · BOOTSTRAP · AGENTS · ENV
│                       #   · REWARD_SCHEMA · REGISTRY_AUTH · RELATED_WORK
├── contributing/ADDING_A_PIPELINE.md
└── release_notes/      #   HISTORY.md + per-release deep dives
.github/workflows/      # ci.yml (lint + 3.12/3.13/3.14 matrix + build) · release.yml
```

## Local artifacts (all gitignored)

The tool writes everything under **`./workspace/`** by default; `--out` and
`R2E_CACHE_DIR` override it. Nothing there is committed, and it's all
regenerable — but at very different cost:

- **`workspace/bootstrap/`** — per-repo Docker env cache, content-addressed on the
  bootstrap options. Each entry costs real LLM spend (~$3–8/repo) to rebuild.
  Delete this last. Override with `R2E_CACHE_DIR`.
- **Dataset stagings** — cheap; the canonical copies live on the HF Hub and come
  back with `repo2rlenv pull`.
- **`harbor run` output, fetched diffs, agent transcripts** — free or cheap to re-fetch.

`plans/` and `references/` are also gitignored: internal working notes and shallow
clones of the inspiration repos (SWE-bench, SWE-smith, R2E-Gym, Magicoder, …). They
are per-developer and won't exist in a fresh clone — don't write code that assumes
either is present.

## UI conventions

```python
from repo2rlenv.ui import console

console.success("emitted task X")          # ✓ green
console.info("starting pipeline...")        # ⓘ cyan
console.warn("smoke gate exited 5")         # ⚠ yellow
console.error("docker daemon down")         # ✗ red
console.kv({"reward": 0.98, ...}, title="...")    # panel with key/value table
with console.section("Pushing to Hub"): ... # bracketed rule
```

For long-running work with a redrawing display, add a view under `ui/views/`:

```python
with BootstrapView(...) as view:
    ensure_bootstrap(..., on_turn=view.on_turn,
                          on_phase=view.on_phase,
                          on_thinking=view.on_thinking,
                          on_executing=view.on_executing)
    view.set_outcome(success=True, ...)
```

Logging routes through `RichHandler` via `install_logging()` (called from
`cli.py:main()`). Noisy loggers (litellm / httpx / anthropic / openai) are
auto-suppressed to WARNING while a Live is active.

## Input sources + auth

`--repo` accepts a GitHub `owner/name`, a `gitlab.com` URL, or a local path
(`/abs`, `./rel`, `~`, `file://` — canonicalized to `file://<abspath>`).
`RepoSpec.source_kind` classifies it; `sources.py` defines a `Capability`
(pull_requests / issues / commit_api) per source and per pipeline's
`required_capabilities`. `cmd_generate` gates incompatible combinations up front —
e.g. `pr_diff` against a local path is refused with a clear error.

- Any source: `commit_runtime`, `code_instruct`, `equivalence_tests`
- GitHub **or** GitLab: `pr_diff`, `pr_runtime` (gitlab.com MRs via
  `gitlab.py`, dispatched by `provider.py`)
- GitHub only: `cve_patches` (needs OSV + the GitHub commit API)

`auth.resolve_repo_token()` dispatches by source: local → `None` (no `gh`
shell-out); gitlab → `repo.auth_token_env` then `$GITLAB_TOKEN`; github →
`resolve_github_token()`, which tries in order:

1. `repo.auth_token_env` env var (if set in config)
2. `gh auth token` (default — works for anyone who has run `gh auth login`)
3. `$GITHUB_TOKEN`
4. `None` (anonymous; fails on private repos)

HF Hub auth uses `huggingface_hub`'s own resolution
(`~/.cache/huggingface/token` or `$HF_TOKEN`). LLM keys come from provider-default
env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, …) via `auth.resolve_llm_api_key()`.

Registry push credentials resolve from explicit env vars first: GHCR reads
`GHCR_TOKEN` / `GITHUB_TOKEN` (needs a one-time
`gh auth refresh -h github.com -s write:packages`); Docker Hub reads
`DOCKER_USERNAME` + `DOCKER_TOKEN` (a PAT — the credstore's OAuth token is
pull-only) and pushes under the Docker Hub *user's* namespace. ECR and Google AR
have their own modules. Multi-repo datasets push one image per repo and rewrite
each task to its own digest. Details: `docs/reference/REGISTRY_AUTH.md`; full env
var list: `docs/reference/ENV.md`.

## Cost tracking

`llm.complete()` returns `LLMResponse.cost_usd` via
`litellm.completion_cost(response)`, accumulated into
`AgentOutcome.total_cost_estimate_usd` and `BootstrapResult.llm_cost_estimate_usd`.
`BootstrapSpec.max_llm_spend_usd` is enforced inside `agent.py:run_agent_loop` —
at `total_cost ≥ max_spend_usd` the loop short-circuits with
`success=False, reason="cost budget exceeded: ..."`. The runner logs the budget at
startup.

Any change that adds LLM calls should thread cost through these fields rather than
introducing a parallel counter.

## Tests

`uv run pytest -q` is the canonical command; CI runs it on Python 3.12 / 3.13 / 3.14.
Conventions (fixtures over mocks, specific exception types, where files live) are in
[`CONTRIBUTING.md`](./CONTRIBUTING.md#tests). Two things worth knowing before you
debug a red run:

- **Some tests are opt-in and skip by default** — they need live network or
  credentials, and are gated behind env vars (`R2E_LIVE_PROBE_GHCR`,
  `R2E_E2E_HUB_BUILD`, `R2E_LIVE_GITLAB`). Skips here are expected.
- **`tests/test_e2e_public.py::test_e2e_public_trl` is flaky by construction.** It
  hits the live GitHub API and asserts that ≥1 task is emitted from the two most
  recent PRs on `huggingface/trl`, so it fails with
  `skip_reasons={'diff_too_small': N}` whenever those PRs happen to be small. That
  is not a regression — don't chase it.

## Design decisions

1. **No bind mounts for the bootstrap container** — the repo is `docker cp`'d in so
   `docker commit` captures it. Bind mounts are runtime overlays and aren't included
   in committed images. `bootstrap/docker.py:DockerSandbox.start`.
2. **Lenient smoke gate** — pytest exit codes 0, 1 and 5 all mean "env works"
   (1 = tests ran and failed; 5 = nothing collected). Only 2+ is a real env problem.
   `bootstrap/runner.py`.
3. **Test commands are joined with `&&`** — `test_cmds` runs as a single bash
   invocation so `export PATH=…` carries into the next command. Don't loop
   `sandbox.exec()` over the list.
4. **`registry_url` uses Harbor's legacy format** — we publish a `registry.json`
   that `harbor download --registry-url <url>` consumes directly, so no Harbor
   patches are needed.
5. **No `repo2rlenv run`, no parallel sandbox runtime** — running full tasks is
   `harbor run`'s job. This project is synthesis-only.
6. **Multi-repo datasets work in both push modes** — `inline` bakes each task's own
   bootstrap recipe from the verified `rebuild_cmds` (NOT the agent transcript);
   `registry` pushes each distinct image and rewrites each task to its digest.
   `push` clean-syncs `tasks/**` (a deleted task disappears from the Hub) and
   auto-writes `manifest.json`. Runtime pipelines ship
   `tests/{verifier.py,f2p.json,p2p.json}` as plain files — Harbor mounts `tests/`
   at `/tests`, so no base64 in `test.sh`.
7. **The environment enforces, the prompt never asks** — `_env_guard.py` scrubs git
   history back to `base_commit` and blackholes PyPI/GitHub egress for every emitted
   task, because a sandbox-verified task is otherwise gameable by fetching the
   published fix. Background: `docs/release_notes/HISTORY.md`.

## External dependencies

- **Harbor** (`uv tool install harbor`) — runs the generated tasks. We deliberately
  don't ship a parallel runtime.
- **Docker** — required for `bootstrap` and every `_runtime` pipeline. `pr_diff`
  *generates* without it, though its emitted task *runs* in Docker (thin
  `python:3.12-slim`); set `emit_harbor_env=False` for pure text output.
- **LiteLLM** — the single client across providers (Anthropic / OpenAI / HF Router /
  Bedrock / Ollama / vLLM).
- **Rich** — every CLI surface; foundation of `ui/`.
- **huggingface_hub** — dataset publish/pull.
- **`gh` CLI** — clone + PR listing; the least-friction GitHub auth path.

## Common commands

```bash
# Generate a sandbox-verified dataset (auto-triggers bootstrap if needed)
uv run repo2rlenv generate \
  --repo <owner>/<repo> --pipeline pr_runtime \
  --pipeline-opt limit=10 \
  --llm anthropic/claude-sonnet-4-6 \
  --out ./workspace/datasets/<name>

# Validate a dataset (fast structural check — no LLM, no Docker)
uv run repo2rlenv validate ./workspace/datasets/<name>

# Publish / retrieve (a bare name resolves its owner via whoami)
uv run repo2rlenv push ./workspace/datasets/<name> <org>/<name>
uv run repo2rlenv pull <org>/<name>

# Everything CI runs
uv run pytest -q && uv run ruff check . && uv run ruff format --check .

# Dependencies
uv add <pkg>            # runtime
uv add --dev <pkg>      # dev only
```

## Status

Version is whatever `pyproject.toml` says (`__version__` reads package metadata).
6 pipelines registered — 3 stable, 3 experimental; see the table above and
[`docs/release_notes/HISTORY.md`](./docs/release_notes/HISTORY.md) for how it got here.
