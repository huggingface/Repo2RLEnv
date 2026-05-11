# CLAUDE.md — project memory for Repo2RLEnv

This file is auto-loaded by Claude Code in this repo. Keep it tight; longer prose belongs in `docs/`.

## What this is

**Repo2RLEnv** (`repo2rlenv` on PyPI) turns any GitHub repository into a verifiable RL training/eval dataset. End-to-end: **synthesis → standardize → train + eval**, focus on training. We emit datasets in the [Harbor](https://github.com/harbor-framework/harbor) task format so they drop straight into Harbor's runtime ecosystem (Local Docker / Modal / Daytona / E2B / Runloop + 22 agent harnesses).

GitHub: https://github.com/adithya-s-k/Repo2RLEnv · PyPI: `repo2rlenv` · License: Apache-2.0.

## Architecture

Three layers, only the first is ours:

| Layer | We ship | We delegate |
|---|---|---|
| **Generation** (pipelines that produce tasks) | `src/repo2rlenv/pipelines/` — the moat | — |
| **Spec** (uniform output format) | `[metadata.repo2env]` extension to Harbor's `task.toml` | Harbor's task spec |
| **Consumption** (sandboxes / agents / runtime) | HF Hub publish bridge; planned TRL trainer bridge | Harbor's full stack |

## Where things live

```
src/repo2rlenv/
├── spec/                       # Pydantic input + output models (the contract)
├── pipelines/
│   ├── base.py                 # Pipeline Protocol + PipelineResult
│   ├── pr_mining_lite.py       # SHIPPED — SWE-RL-style text-only PR mining
│   └── <future pipelines>
├── bootstrap/                  # v0.2 — LLM-driven Docker env generation
│   ├── runner.py               # ensure_bootstrap() orchestrator
│   ├── agent.py                # ReAct loop
│   ├── docker.py               # DockerSandbox primitives
│   ├── language.py             # auto-detect Python/JS/Go/Rust/...
│   └── cache.py                # filesystem cache under ./envs/
├── ui/                         # Unified Rich UI module (every CLI uses this)
│   ├── console.py              # singleton R2EConsole + install_logging()
│   ├── theme.py                # one place for colors + glyphs
│   ├── primitives.py           # success_panel, error_panel, kv_panel, ...
│   ├── live.py                 # live_view() context manager
│   └── views/
│       ├── bootstrap.py        # BootstrapView (split-panel live display)
│       └── generation.py       # GenerationView (progress bar + stats)
├── emitter/harbor.py           # Task → Harbor task.toml directory writer
├── hub.py                      # push to HF Hub + Harbor-compatible registry.json
├── reward.py                   # SWE-RL-style diff-similarity reward (stdlib only)
├── llm.py                      # LiteLLM wrapper + LiteLLM completion_cost tracking
├── github.py                   # `gh` CLI wrapper for PR list + diff fetch
├── auth.py                     # gh CLI → env var token resolution
├── config.py                   # YAML/TOML config loader
└── cli.py                      # argparse entry points

docs/                           # public docs (committed)
├── SPEC.md · AUTH.md · API.md · AGENTS.md · BOOTSTRAP.md · README.md
└── pipelines/                  # per-pipeline docs with Mermaid flowcharts

plans/                          # internal working docs (gitignored)
references/                     # cloned inspiration repos (gitignored)
envs/, envs-*/, .r2e_cache/     # local artifacts (gitignored)
tests/                          # pytest; 66/66 pass as of v0.2
```

## Pipeline contract

Every synthesis pipeline implements `repo2rlenv.pipelines.base.Pipeline`:

```python
class Pipeline(Protocol):
    name: ClassVar[PipelineName]
    def __init__(self, input: GenerationInput, options: BaseModel) -> None: ...
    def run(self, out_dir: Path) -> PipelineResult: ...
```

The conformance test (`tests/test_pipeline_contract.py`) fails for any registered pipeline that doesn't conform. **Always run** `uv run pytest tests/test_pipeline_contract.py` after touching anything in `pipelines/`.

Adding a new pipeline: see [`docs/pipelines/ADDING_A_PIPELINE.md`](./docs/pipelines/ADDING_A_PIPELINE.md).

## UI conventions (use the shared module, not raw prints)

**Never use `print()`** in CLI code. Always:

```python
from repo2rlenv.ui import console

console.success("emitted task X")          # ✓ green
console.info("starting pipeline...")        # ⓘ cyan
console.warn("smoke gate exited 5")         # ⚠ yellow
console.error("docker daemon down")         # ✗ red
console.kv({"reward": 0.98, ...}, title="...")    # panel with key/value table
with console.section("Pushing to Hub"): ... # bracketed rule
```

For long-running tasks with a redrawing display, build a view in `src/repo2rlenv/ui/views/`. Pattern:

```python
with BootstrapView(...) as view:
    ensure_bootstrap(..., on_turn=view.on_turn,
                          on_phase=view.on_phase,
                          on_thinking=view.on_thinking,
                          on_executing=view.on_executing)
    view.set_outcome(success=True, ...)
```

Logging is routed through `RichHandler` via `install_logging()` (called from `cli.py:main()`). Noisy library loggers (litellm/httpx/anthropic/openai) are auto-suppressed to WARNING while a Live is active.

## Auth resolution chain (GitHub)

`auth.resolve_github_token()` checks in order:
1. `repo.auth_token_env` env var (if explicitly set in config)
2. `gh auth token` (default — works for any user who's `gh auth login`'d)
3. `$GITHUB_TOKEN`
4. None (anonymous; fails for private repos)

HF Hub auth is the `huggingface_hub` library's auto-resolution (`~/.cache/huggingface/token` or `$HF_TOKEN`). LLM keys via provider-default env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, ...) — see `auth.resolve_llm_api_key()`.

For GHCR push: needs `gh auth refresh -h github.com -s write:packages` (one-time, the user does this).

## Cost tracking

`llm.complete()` returns `LLMResponse.cost_usd` via `litellm.completion_cost(response)` — uses LiteLLM's built-in model_cost map. Accumulated in `AgentOutcome.total_cost_estimate_usd` and `BootstrapResult.llm_cost_estimate_usd`. `BootstrapSpec.max_llm_spend_usd` is declared as a guardrail — **not yet enforced** (open TODO).

## Conventions for changes

- **`uv add <pkg>`** for dependencies. Never hand-edit `pyproject.toml`'s `dependencies` array.
- **No `Co-Authored-By: Claude` trailer** on commits. User explicitly rejected it; see `~/.claude/projects/.../memory/feedback_no_coauthor.md`.
- **Commits**: terse subject + short body explaining "why". Don't reference the current task; that goes in the PR description.
- **PRs**: title under 70 chars; description has summary + test plan + out-of-scope items. Close issues via `Closes #N` in commit body.
- **Tests**: every code change should keep the suite green. `uv run pytest -q` is the canonical command. 66/66 must pass.
- **Acknowledgments**: when a file draws inspiration from external work, add a header block crediting the source repo + paper + license + clarifying our license posture. See `bootstrap/__init__.py` or `reward.py` for the format.

## Cheatsheet — common tasks

```bash
# Run a full bootstrap with the live UI (interactive terminal only)
./demo_bootstrap.sh

# Generate a small dataset, push to HF Hub
uv run repo2rlenv generate \
  --repo huggingface/trl --pipeline pr_mining_lite \
  --pipeline-opt limit=5 \
  --llm anthropic/claude-sonnet-4-6 \
  --out hf://AdithyaSK/trl-r2e-v0-1

# Score a candidate diff
uv run repo2rlenv reward --task ./out/some-task --prediction ./candidate.diff

# Validate a dataset
uv run repo2rlenv validate ./datasets/django

# Run all tests
uv run pytest -q

# Add a dep
uv add <pkg>            # runtime
uv add --dev <pkg>      # dev only
```

## Key external dependencies

- **Harbor** (`uv tool install harbor`) — runs our generated tasks. We don't ship a parallel runtime.
- **Docker** — required for the `bootstrap` phase and any sandbox-required pipeline. Lite pipelines work without it.
- **LiteLLM** — single client across all LLM providers (Anthropic / OpenAI / HF Router / Bedrock / Ollama / vLLM).
- **Rich** — every CLI surface; foundation of `src/repo2rlenv/ui/`.
- **huggingface_hub** — dataset publish/pull; auto-resolves token from `~/.cache/huggingface/token`.
- **`gh` CLI** — clone + PR listing. The path of least friction for GitHub auth.

## Notable design decisions

1. **No bind mounts for the bootstrap container** — we `docker cp` the repo files into the container so `docker commit` captures them. Bind mounts are runtime overlays and aren't included in committed images. See `bootstrap/docker.py:DockerSandbox.start`.
2. **Lenient smoke gate** — pytest exit codes 0, 1, and 5 are all "env works" (1 = tests failed but ran; 5 = no tests collected). Only 2+ flags a real env problem. See `bootstrap/runner.py`.
3. **Test commands joined with `&&`** — `test_cmds` is a list run as one bash invocation so `export PATH=...` carries over to the next command. Don't iterate the list with `sandbox.exec(each)`.
4. **registry_url uses Harbor's legacy format** — we publish a `registry.json` to HF Hub that `harbor download --registry-url <hf-url>` can consume directly. No Harbor patches needed.
5. **No `repo2rlenv run` / no parallel sandbox runtime** — for full tasks, users run `harbor run`. We're synthesis-only.

## Status (May 2026)

- **v0.1.0 shipped** on PyPI: `pr_mining_lite` + HF Hub publish + diff-similarity reward
- **v0.2 in flight** (PR #2): bootstrap phase, Rich UI module, cost tracking
- **v0.3 planned**: full `pr_mining` with Harbor execution + TRL trainer bridge
- 8 more pipelines planned in `docs/pipelines/` (commit_mining, mutation, oss_instruct, equivalence_tests, live_pr_mining, cve_mining, refactor_synthesis, plus `pr_mining` full)

If anything in this file conflicts with the actual code, **trust the code** and fix this file.
