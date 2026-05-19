# CLAUDE.md — project memory for Repo2RLEnv

This file is auto-loaded by Claude Code in this repo. Keep it tight; longer prose belongs in `docs/`.

## What this is

**Repo2RLEnv** (`repo2rlenv` on PyPI) turns any GitHub repository into a verifiable RL training/eval dataset. End-to-end: **synthesis → standardize → train + eval**, focus on training. We emit datasets in the [Harbor](https://github.com/harbor-framework/harbor) task format so they drop straight into Harbor's runtime ecosystem (Local Docker / Modal / Daytona / E2B / Runloop + 22 agent harnesses).

GitHub: https://github.com/huggingface/Repo2RLEnv · PyPI: `repo2rlenv` · License: Apache-2.0.

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
│   ├── pr_diff.py              # SHIPPED — text-only PR mining (was: pr_mining_lite)
│   └── <future pipelines>      # pr_runtime, commit_runtime, mutation_bugs, ...
├── bootstrap/                  # v0.2 — LLM-driven Docker env generation
│   ├── runner.py               # ensure_bootstrap() orchestrator
│   ├── agent.py                # ReAct loop
│   ├── docker.py               # DockerSandbox primitives
│   ├── language.py             # auto-detect Python/JS/Go/Rust/...
│   └── cache.py                # filesystem cache under ./envs/ (keyed on opts)
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

docs/                           # public docs (committed), three tiers:
├── README.md                   #   index
├── quickstart.md               #   install → first dataset → push, 10 min
├── reference/                  #   stable contracts + module-level API
│   └── SPEC.md · API.md · AUTH.md · BOOTSTRAP.md · AGENTS.md
├── pipelines/                  #   one page per synthesis pipeline
│   └── README.md · pr_diff.md · pr_runtime.md · ...
└── contributing/
    └── ADDING_A_PIPELINE.md    #   cookbook for shipping a new pipeline

plans/                          # internal working docs (gitignored)
references/                     # cloned inspiration repos (gitignored)
envs/, envs-*/, .r2e_cache/     # local artifacts (gitignored)
tests/                          # pytest; 620/620 pass as of v0.8.2.post3
.github/workflows/              # ci.yml (lint + matrix tests + build),
                                # release.yml (PyPI publish on tagged release)
CONTRIBUTING.md                 # dev setup, PR conventions, release flow
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

`llm.complete()` returns `LLMResponse.cost_usd` via `litellm.completion_cost(response)` — uses LiteLLM's built-in model_cost map. Accumulated in `AgentOutcome.total_cost_estimate_usd` and `BootstrapResult.llm_cost_estimate_usd`. `BootstrapSpec.max_llm_spend_usd` is enforced inside the agent loop (`agent.py:run_agent_loop`): when running `total_cost ≥ max_spend_usd`, the loop short-circuits with `success=False, reason="cost budget exceeded: ..."`. The runner logs the configured budget at startup.

## Conventions for changes

The canonical contributor reference is [`CONTRIBUTING.md`](./CONTRIBUTING.md) at the repo root — read it before non-trivial changes. The high-leverage bits:

- **`uv add <pkg>`** for dependencies. Never hand-edit `pyproject.toml`'s `dependencies` array.
- **No `Co-Authored-By: Claude` trailer** on commits. User explicitly rejected it; see `~/.claude/projects/.../memory/feedback_no_coauthor.md`.
- **Commits**: terse subject + short body explaining "why". Don't reference the current task; that goes in the PR description.
- **PRs**: title under 70 chars; description has summary + test plan + out-of-scope items. Close issues via `Closes #N` in commit body.
- **Tests**: every code change should keep the suite green. `uv run pytest -q` is the canonical command. **620/620 must pass.**
- **Lint + format**: `uv run ruff check .` and `uv run ruff format .` before commit. CI rejects unformatted code or lint violations.
- **Acknowledgments**: when a file draws inspiration from external work, add a header block crediting the source repo + paper + license + clarifying our license posture. See `bootstrap/__init__.py` or `reward.py` for the format.

## CI / CD (GitHub Actions)

Two workflows under `.github/workflows/`:

- **`ci.yml`** — runs on every push to `main` + every PR. Three jobs: `lint` (ruff check + format check), `test` matrix over Python **3.12 / 3.13 / 3.14**, and `build` (uv build sdist + wheel + smoke install). All three must pass for a merge.
- **`release.yml`** — fires on GitHub Release `published` events. Re-runs the full test matrix on the tag, builds, publishes to PyPI via `PYPI_API_TOKEN` repo secret, attaches sdist + wheel to the Release page. Manual re-runs via `workflow_dispatch -f tag=vX.Y.Z` if a publish step fails.

Cutting a release (full flow):

1. Bump `version` in `pyproject.toml`. `__version__` reads from `importlib.metadata` so no other code change needed.
2. Commit + push to `main`. CI confirms tests green.
3. `git tag vX.Y.Z && git push origin vX.Y.Z`
4. `gh release create vX.Y.Z --generate-notes` (or web UI)
5. `release.yml` auto-publishes to PyPI and attaches artifacts to the Release.

`PYPI_API_TOKEN` lives in repo secrets ONLY. Don't paste it into chat, `.env`, or any committed file. The `pypi` GitHub environment is in place but currently has no protection rules — adding a deployment approval rule there is a one-click upgrade if we ever want a manual gate before publish.

## Cheatsheet — common tasks

```bash
# Run a full bootstrap with the live UI (interactive terminal only)
./demo_bootstrap.sh

# Full end-to-end demo: bootstrap → generate → validate → harbor (oracle + opencode×2)
./demo_e2e.sh                                # default: pallets/click
REPO=pocketbase/pocketbase ./demo_e2e.sh     # different repo

# Generate a sandbox-verified dataset (auto-triggers bootstrap if needed)
uv run repo2rlenv generate \
  --repo <owner>/<repo> --pipeline pr_runtime \
  --pipeline-opt limit=10 \
  --llm anthropic/claude-sonnet-4-6 \
  --out ./datasets/<dataset-name>

# Validate a dataset (fast structural check, no LLM, no Docker)
uv run repo2rlenv validate ./datasets/<dataset-name>

# Publish to HF Hub (bare name auto-resolves owner via whoami)
uv run repo2rlenv push ./datasets/<dataset-name> <your-org>/<dataset-name>

# Pull a published dataset back
uv run repo2rlenv pull <your-org>/<dataset-name>

# Run all tests + lint + format check (everything CI runs)
uv run pytest -q
uv run ruff check .
uv run ruff format --check .

# Add a dep
uv add <pkg>            # runtime
uv add --dev <pkg>      # dev only
```

## Key external dependencies

- **Harbor** (`uv tool install harbor`) — runs our generated tasks. We don't ship a parallel runtime.
- **Docker** — required for the `bootstrap` phase and any `_runtime` pipeline. `_diff` pipelines (text-only) work without it.
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

- **v0.1.0** shipped on PyPI: `pr_diff` (originally `pr_mining_lite`) + HF Hub publish + diff-similarity reward
- **v0.2** merged into main: bootstrap phase, Rich UI module, cost tracking, content-addressed cache keyed on bootstrap options
- **v0.3.0** shipped on PyPI: `pr_runtime` (sandbox-verified PR mining) + auto-trigger bootstrap from `generate` + structural quality filters + targeted test invocation + CI/CD (ruff + matrix tests + release workflow)
- **v0.4.0** shipped on PyPI: polyglot log parsers (Go / Cargo / Jest) + Harbor end-to-end compliance fixes (task.name format, solve.sh shim, /logs/verifier/reward.txt, PATH prelude for non-Python toolchains, defensive git install)
- **v0.5.0** shipped on PyPI: `pr_stream` (continuous PR mining with watermark state) + `commit_runtime` (commit-level mining, SWE-GEN style). Both Harbor-verified.
- **v0.6.0** shipped on PyPI: first LLM-synthesized pipelines — `mutation_bugs` (procedural AST bug injection inspired by SWE-smith) + `code_instruct` (repo-anchored OSS-Instruct with executable verifiers, inspired by Magicoder). Both Harbor-verified on `pallets/click` (Mean reward 1.000).
- **v0.7.0** shipped on PyPI: `equivalence_tests` (R2E-style function-level synthesis — extract real function, LLM writes equivalence test against `reference_<name>` oracle, gold patch fills the candidate) + `cve_patches` (OSV-driven CVE→fix-commit pipeline, reuses pr_runtime validation harness). Both Harbor-verified.
- **v0.8.0** shipped on PyPI: `refactor_synthesis` (Python-native rename-refactor mining — drops the v1.0-planned JVM RefactoringMiner dep; commit-message regex + diff verification + multi-criteria structural+behavioral verifier). Harbor-verified on `pallets/click` (Mean reward 1.000). **All 9 pipelines now shipped. 622/622 tests pass.**
- **v0.9 planned**: LLM-judged QA gate (SWE-Bench++ four-layer recipe), iterative refinement loop for `equivalence_tests`, LLM-synthesized PoC for `cve_patches`, Extract Method / Inline kinds for `refactor_synthesis`, HF Hub append-mode for `pr_stream`, polyglot mutation (tree-sitter)
- No more pipelines planned in `docs/pipelines/` — see [`docs/pipelines/README.md`](./docs/pipelines/README.md) for the full table

### Naming convention (post-rename)

Pipelines follow `{source}_{shape}`:
- `_diff` — text-only, no sandbox (e.g. `pr_diff`)
- `_runtime` — runs inside the bootstrap sandbox to verify the oracle (e.g. `pr_runtime`, `commit_runtime`)
- `_stream` — continuous mining variant
- `_bugs` / `_patches` / `_instruct` / `_tests` / `_synthesis` — name of the artifact type for synthesized pipelines

If anything in this file conflicts with the actual code, **trust the code** and fix this file.
