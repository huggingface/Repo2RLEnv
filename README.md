

<p align="center">
  <h1 align="center">Repo2RLEnv</h1>
  <p align="center"><b>Turn any GitHub repository into a verifiable RL environment for training and evaluation.</b></p>
</p>

<p align="center">
  <a href="https://pypi.org/project/repo2rlenv/"><img alt="PyPI" src="https://img.shields.io/pypi/v/repo2rlenv?color=blue"></a>
  <a href="https://pypi.org/project/repo2rlenv/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/repo2rlenv"></a>
  <a href="https://github.com/huggingface/Repo2RLEnv/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/huggingface/Repo2RLEnv/actions/workflows/ci.yml/badge.svg"></a>
  <a href="./LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache%202.0-green"></a>
  <a href="https://github.com/harbor-framework/harbor"><img alt="Harbor" src="https://img.shields.io/badge/spec-Harbor-FFD21F"></a>
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> ·
  <a href="#pipelines">Pipelines</a> ·
  <a href="#what-you-get-out">Output</a> ·
  <a href="#documentation">Docs</a>
</p>

<p align="center">
  <img src="assets/banner.png" alt="Repo2RLEnv — turn any repo into verifiable RL environments" width="100%">
</p>

Repo2RLEnv synthesizes **verifiable** training and evaluation data from existing repositories, exports it into a uniform spec, and pushes it straight to the Hugging Face Hub. The output spec is [Harbor](https://github.com/harbor-framework/harbor)'s, so every dataset you produce drops directly into any Harbor-compatible runtime — no glue code.


## Quickstart

```bash
# Install (pick one)
uv add repo2rlenv                                 # add to a uv-managed project
uvx repo2rlenv --help                             # one-shot, no install
pip install repo2rlenv                            # classic

# Auth: nothing to set up if you've done `gh auth login` and `huggingface-cli login`.
# Otherwise:  export GITHUB_TOKEN=... ; export HF_TOKEN=...

# Generate a dataset locally
repo2rlenv generate \
  --repo <owner>/<repo> \
  --pipeline pr_runtime \
  --pipeline-opt limit=5 \
  --llm anthropic/claude-sonnet-4-6 \
  --out ./datasets/<dataset-name>

# Validate (fast structural check) and publish
repo2rlenv validate ./datasets/<dataset-name>
repo2rlenv push ./datasets/<dataset-name> <your-org>/<dataset-name>

# Anyone can pull + run a published dataset on a fresh machine
repo2rlenv pull <your-org>/<dataset-name> ./datasets/<dataset-name>
harbor run -p ./datasets/<dataset-name> -a oracle --env docker
```

Full walkthrough in [**`docs/quickstart.md`**](./docs/quickstart.md).

## How it works

Repo2RLEnv runs **synthesis pipelines** that read real repositories — source code, merged PRs, commits, CVEs — and use them as a *seed* to generate RL environments: tasks with a concrete, solvable objective and a programmatic reward (no human grading).

**Input: any repo. Output: a runnable RL environment** you can point any LLM or coding agent at.

```python
# every pipeline shares one contract: read a repo, emit verifiable tasks
class Pipeline(Protocol):
    name: ClassVar[PipelineName]
    def run(self, out_dir: Path) -> PipelineResult: ...   # writes tasks/<id>/
```

Generate from a repo, then run any agent against the result — the environment is scored automatically:

```bash
# 1. synthesize an environment from a repo
repo2rlenv generate --repo pallets/click --pipeline pr_runtime \
  --pipeline-opt limit=10 --llm anthropic/claude-sonnet-4-6 --out ./env-click

# 2. run an agent inside the sandbox (swap -a / -m for any of 25+ harnesses)
export ANTHROPIC_API_KEY=...   OPENAI_API_KEY=...
harbor run -p ./env-click -a claude-code -m anthropic/claude-sonnet-4-6 --ae ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY --env docker
harbor run -p ./env-click -a openhands   -m openai/gpt-4o               --ae OPENAI_API_KEY=$OPENAI_API_KEY     --env docker
harbor run -p ./env-click -a codex       -m openai/o3                   --ae OPENAI_API_KEY=$OPENAI_API_KEY     --env docker
harbor run -p ./env-click -a hermes      -m anthropic/claude-sonnet-4-6 --ae ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY --env docker
```

Each agent's per-task reward lands in `/logs/verifier/reward.json`, ready for training or eval.

## Pipelines

A pipeline turns a repo into Harbor tasks. **Two are stable** and recommended for production; **six are experimental** — usable today (the CLI prints a warning before they run), with interfaces and output quality still evolving.

### Stable

**[`pr_diff`](./docs/pipelines/pr_diff.md)** mines merged pull-request diffs into lightweight, text-only tasks. The agent proposes an edit, and a verifier scores it against the real merged diff — on format, the files it touched, how much it changed, and (via an LLM judge) whether it's semantically right. No per-repo setup: every task ships a thin `python:3.12-slim` image.
→ Reference dataset: [`AdithyaSK/repo2rlenv-pr-diff`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-diff) (100 oracle-verified tasks).

**[`pr_runtime`](./docs/pipelines/pr_runtime.md)** is the SWE-bench-style flagship. It mines merged PRs and actually runs the repo's test suite inside a Docker sandbox: the tests the PR fixed must go from failing to passing under the gold patch, while the rest keep passing. That makes it the strongest, least-gameable signal of the set.
→ Reference dataset: [`AdithyaSK/repo2rlenv-pr-runtime`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-runtime) (100 oracle-verified tasks).

### Experimental

> These run normally but emit a warning first — pin a release if you depend on them. Each links to its own page; the gist:

- **[`commit_runtime`](./docs/pipelines/commit_runtime.md)** — mines commit history directly, catching fixes that never went through a PR. Reference dataset: [`AdithyaSK/repo2rlenv-commit-runtime`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-commit-runtime) (52 oracle-verified envs).
- **[`cve_patches`](./docs/pipelines/cve_patches.md)** — security tasks from public CVEs, mapped to their fix commits.
- **[`mutation_bugs`](./docs/pipelines/mutation_bugs.md)** — injects synthetic bugs into real code; the agent must restore the tests to green.
- **[`code_instruct`](./docs/pipelines/code_instruct.md)** — generates a problem + executable verifier from a real source file.
- **[`equivalence_tests`](./docs/pipelines/equivalence_tests.md)** — the agent reimplements a real function; generated tests check it matches the original.
- **[`refactor_synthesis`](./docs/pipelines/refactor_synthesis.md)** — mines refactor commits and verifies behavior is preserved.

### At a glance

| Pipeline | Stability | Sandbox | LLM use | Languages |
|---|:-:|:-:|---|---|
| `pr_diff` | stable | thin | at verify — judges the solution | any |
| `pr_runtime` | stable | ✅ | at env build — one-time, cached | Py · Go · Node · Rust |
| `commit_runtime` | experimental | ✅ | at env build — one-time, cached | Py · Go · Node · Rust |
| `cve_patches` | experimental | ✅ | at env build — one-time, cached | Py · Go · Node · Rust |
| `mutation_bugs` | experimental | ✅ | at synthesis — writes the task | Py |
| `code_instruct` | experimental | ✅ | at synthesis — writes the task | Py |
| `equivalence_tests` | experimental | ✅ | at synthesis — writes the task | Py |
| `refactor_synthesis` | experimental | ✅ | at env build — one-time, cached | Py |

**What the columns mean**
- **Sandbox** — whether the task runs inside Docker. `✅` = a per-repo image is built once by the [bootstrap phase](#bootstrap) and cached; `thin` = no bootstrap, just a generic `python:3.12-slim` image.
- **LLM use** — *when* a language model is invoked, which sets where your API cost goes:
  - **at env build** — only during bootstrap (constructing the Docker image); cached, so generation itself is LLM-free.
  - **at synthesis** — the model authors the task (problem + verifier) for every task generated.
  - **at verify** — the model judges the agent's solution at scoring time (one reward component), and degrades gracefully when no key is set.
- **Languages** — source languages the pipeline supports.

→ **Full reference** — per-pipeline options, reward design, and dataset cards: [**`docs/pipelines/`**](./docs/pipelines/README.md).

## Bootstrap

Sandbox pipelines need a working Docker environment for the target repo. Repo2RLEnv's **bootstrap phase** builds it automatically — an LLM agent iterates shell commands inside a fresh container until the repo builds and its test suite collects, then commits and content-addresses the image. The expensive step runs **once per (repo, ref)**; every downstream task reuses the cache. `pr_diff` skips it entirely.

```bash
repo2rlenv bootstrap --repo <owner>/<repo> --llm anthropic/claude-sonnet-4-6
```

Design, cache layout, cost tracking: [`docs/reference/BOOTSTRAP.md`](./docs/reference/BOOTSTRAP.md).

## What you get out

A dataset that:

- **Is verifiable** — every task carries an executable test (`test_execution`) or a stored oracle diff (`diff_similarity`); your trainer picks the reward type.
- **Is content-addressed** — a `content_hash` over each task; identical artifacts ⇒ identical hash.
- **Trains anywhere via Harbor** — TRL, SkyRL, Prime-RL, Tinker, Miles, Slime, harbor.rl.
- **Evaluates with any agent harness** — Claude Code, OpenHands, Codex CLI, Gemini CLI, …
- **Is language-agnostic by spec** — runtime pipelines emit a Dockerfile + shell verifier; `pr_diff` is pure text and works for any language.
- **Publishes natively** to the Hub — `repo2rlenv push` writes a Harbor-compatible `registry.json` so consumers `harbor download` (or `repo2rlenv pull`) with zero glue.
- **Supports private repos** end-to-end — `gh auth token` resolved automatically; build secrets declared by name; verifier-time secrets forbidden by spec.

## Under the hood

Our focus is **synthesis** — we don't reimplement sandboxes, agent harnesses, or a registry. Tasks are emitted in the [Harbor](https://github.com/harbor-framework/harbor) format (with a small `[metadata.repo2env]` block for provenance: pipeline, base commit, PR URL, content hash, reward kinds), so they run on Harbor's existing stack — Local Docker / Modal / Daytona / E2B / Runloop, 25+ agent harnesses, parallel execution, and the publishing CLI.

## Contributing a pipeline

Pipelines are pluggable by design — adding a synthesis strategy is the main way to extend Repo2RLEnv:

1. Implement the `Pipeline` protocol (`name` + `run() -> PipelineResult`) in `src/repo2rlenv/pipelines/`.
2. Register it in `PIPELINES` and add its options model; new pipelines start `experimental = True`.
3. `uv run pytest tests/test_pipeline_contract.py` enforces the contract.

Full cookbook (oracle invariant, reward design, QA gate): [**`docs/contributing/ADDING_A_PIPELINE.md`**](./docs/contributing/ADDING_A_PIPELINE.md). Issues and PRs welcome — see [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## Documentation

- 🚀 [**`docs/quickstart.md`**](./docs/quickstart.md) — install → first dataset → push, in 10 minutes
- 📖 [**`docs/pipelines/`**](./docs/pipelines/README.md) — one page per pipeline (when to use, oracle shape, options)
- 📚 Reference contracts:
  - [`SPEC.md`](./docs/reference/SPEC.md) — input/output contract
  - [`API.md`](./docs/reference/API.md) — Python API for `src/repo2rlenv/`
  - [`AUTH.md`](./docs/reference/AUTH.md) — GitHub / HF / LLM auth resolution
  - [`ENV.md`](./docs/reference/ENV.md) — every environment variable the tool reads, in one place
  - [`BOOTSTRAP.md`](./docs/reference/BOOTSTRAP.md) — LLM-iterated per-repo Docker image
  - [`AGENTS.md`](./docs/reference/AGENTS.md) — Harbor agent harnesses + RL trace plumbing
- 🛠 [**`CONTRIBUTING.md`**](./CONTRIBUTING.md) — dev setup, PR conventions, release flow
- 🧪 [**`ADDING_A_PIPELINE.md`**](./docs/contributing/ADDING_A_PIPELINE.md) — cookbook for shipping a new pipeline

## Adjacent projects

- [**Harbor**](https://github.com/harbor-framework/harbor) — the task format + runtime we **adopt** as our output spec
- [**RepoLaunch**](https://github.com/microsoft/RepoLaunch) (Microsoft) — LLM-agent env setup; our `bootstrap` is an independent reimplementation
- [**OpenReward**](https://docs.openreward.ai) — ORS protocol + extra trainer integrations above Harbor
- [**SWE-Gym**](https://github.com/SWE-Gym/SWE-Gym) — RL-environment framing for SWE-bench-style tasks
- [**verifiers**](https://github.com/willccbb/verifiers) (Prime Intellect), [**OpenEnv**](https://github.com/meta-pytorch/OpenEnv) (Meta + HF) — adjacent standardization efforts

Every pipeline that draws from external work carries an Acknowledgment block in its `.py` file. No code is copied — implementations are independent and Apache-2.0 licensed.

## License

[Apache 2.0](./LICENSE). The original PR/commit contents remain under their respective source-repo licenses; datasets redistribute public commits for ML research under fair use.
