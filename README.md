

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
  <a href="https://huggingface.github.io/Repo2RLEnv/"><img alt="Docs" src="https://img.shields.io/badge/docs-live-brightgreen"></a>
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

→ Explore and visualize any Harbor dataset pushed to the Hub: [**Harbor Visualizer**](https://huggingface.co/spaces/HuggingFaceH4/harbor-visualiser)

The [owned recipe and Tasksmith release inventory](docs/pipelines/releases.md)
links the HuggingEnvs datasets, pipeline guides, generation evidence and measured
costs. Generated exports and independently reviewed tasks retain distinct labels.

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

Repo2RLEnv offers **six native pipelines**, **14 experimental research-inspired recipes**, and **Tasksmith** for adaptive PR conversion. Inputs include repositories, PRs, task seeds and reasoning families. Implementation maturity and generated-task quality are separate labels.

### Stable

**[`pr_diff`](./docs/pipelines/pr_diff.md)** mines merged pull-request diffs into lightweight, text-only tasks. The agent proposes an edit, and a verifier scores it against the real merged diff — on format, the files it touched, how much it changed, and (via an LLM judge) whether it's semantically right. No per-repo setup: every task ships a thin `python:3.12-slim` image.
→ Reference dataset: [`AdithyaSK/repo2rlenv-pr-diff`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-diff) (100 oracle-verified tasks).

**[`pr_runtime`](./docs/pipelines/pr_runtime.md)** is the SWE-bench-style flagship. It mines merged PRs and actually runs the repo's test suite inside a Docker sandbox: the tests the PR fixed must go from failing to passing under the gold patch, while the rest keep passing. That makes it the strongest, least-gameable signal of the set.
→ Reference dataset: [`AdithyaSK/repo2rlenv-pr-runtime`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-runtime) (100 oracle-verified tasks).

**[`commit_runtime`](./docs/pipelines/commit_runtime.md)** is `pr_runtime`'s sibling for repos that don't gate fixes behind PRs (squash-merge / direct-to-main / GitLab / local). It mines **commits** directly, runs the repo's tests in a sandbox (same graded F2P/P2P reward), and an LLM rewrites each commit/issue into a clean, leak-free problem statement so the task isn't gameable.
→ Reference dataset: [`AdithyaSK/repo2rlenv-commit-runtime`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-commit-runtime) (100 oracle-verified envs; Opus solves the sampled tasks).

→ All reference datasets: [**Verifiable RL Environments collection**](https://huggingface.co/collections/HuggingEnvs/repo2rlenv-verifiable-rl-environments-6aa82300d7494c050f50508d)

### Experimental

> These run normally but emit a warning first — pin a release if you depend on them. Each links to its own page; the gist:

- **[`cve_patches`](./docs/pipelines/cve_patches.md)** — security tasks from public CVEs, mapped to their fix commits.
- **[`code_instruct`](./docs/pipelines/code_instruct.md)** — generates a problem + executable verifier from a real source file.
- **[`equivalence_tests`](./docs/pipelines/equivalence_tests.md)** — the agent reimplements a real function; generated tests check it matches the original.

### Native pipelines at a glance

| Pipeline | Stability | Source | Reward signal | Sandbox | LLM use | Languages |
|---|:-:|:-:|---|:-:|---|---|
| `pr_diff` | stable | GitHub · GitLab | `diff_similarity` | thin | at verify — judges the solution | any |
| `pr_runtime` | stable | GitHub · GitLab | `test_execution` + `diff_similarity` | ✅ | at env build — one-time, cached | Py · Go · Node · Rust |
| `commit_runtime` | stable | GitHub · GitLab · local | `test_execution` + `diff_similarity` | ✅ | at env build + per-task instruction synthesis | Py · Go · Node · Rust |
| `cve_patches` | experimental | GitHub | `test_execution` + `diff_similarity` | ✅ | at env build — one-time, cached | Py · Go · Node · Rust |
| `code_instruct` | experimental | GitHub · GitLab · local | `test_execution` | ✅ | at synthesis — writes the task | Py |
| `equivalence_tests` | experimental | GitHub · GitLab · local | `test_execution` | ✅ | at synthesis — writes the task | Py |

**What the columns mean**
- **Source** — where `--repo` can point. **`GitHub · GitLab · local`** = a GitHub `owner/name`, a `gitlab.com` URL, **or a local path** (`/abs`, `./rel`, `~`, `file://`); these need only git + source files. **`GitHub · GitLab`** = PR/MR-mining pipelines (work on github.com and gitlab.com, not a bare local clone — no pull/merge requests there). **`GitHub`** = needs the GitHub commit API + OSV CVE data (`cve_patches`). `generate` blocks an unsupported source up front with a clear, actionable error.
- **Reward signal** — the verifiable signal emitted per task. `test_execution` = the repo's own tests gate the reward (F2P/P2P or pytest pass rate); `diff_similarity` = the agent's output is scored against the oracle diff (format, file targeting, region overlap, LLM judge). Pipelines that emit both use `test_execution` as the primary training signal.
- **Sandbox** — whether the task runs inside Docker. `✅` = a per-repo image is built once by the [bootstrap phase](#bootstrap) and cached; `thin` = no bootstrap, just a generic `python:3.12-slim` image.
- **LLM use** — *when* a language model is invoked, which sets where your API cost goes:
  - **at env build** — only during bootstrap (constructing the Docker image); cached, so generation itself is LLM-free.
  - **at synthesis** — the model authors the task (problem + verifier) for every task generated.
  - **at verify** — the model judges the agent's solution at scoring time (one reward component), and degrades gracefully when no key is set.
- **Languages** — source languages the pipeline supports.

→ **Full reference** — per-pipeline options, reward design, and dataset cards: [**`docs/pipelines/`**](./docs/pipelines/README.md).

### Tasksmith and research-inspired recipes

**[Tasksmith](docs/pipelines/tasksmith.md)** investigates a merged PR, builds its
environment, designs the instruction and private verifier, then runs bounded
review and repair. LangGraph orchestrates Pi or OpenCode. Its
[HF_ML_Tasksmith dataset](https://huggingface.co/datasets/HuggingEnvs/HF_ML_Tasksmith)
contains 50 verified tasks from an assisted campaign; Sonnet solved 19.

Tasksmith and all 14 recipes below are **experimental**. Use the
[route guide](docs/pipelines/owned_recipes.md) for CLI family/recipe names and
[the prompt guide](docs/pipelines/prompt_reference.md) to follow each model call.

| Recipe | Task shape | Reward | Reference dataset |
|---|---|---|---|
| [**swe_smith**](docs/pipelines/repo_mutate.md) | Repair a deliberately introduced source defect | Private tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-smith) |
| [**swe_gen**](docs/pipelines/pr_to_env.md) | Implement the behavior of a supplied merged PR | Private tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-gen) |
| [**swe_flow**](docs/pipelines/repo_reconstruct.md) | Reconstruct functions in dependency order | Private tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-flow) |
| [**r2e**](docs/pipelines/r2e.md) | Implement a function equivalent to a private reference | Equivalence tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-r2e) |
| [**swe_next**](docs/pipelines/swe_next.md) | Repair a task mined from PR history | Private tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-next) |
| [**r2e_gym**](docs/pipelines/r2e_gym.md) | Repair a task mined from commit history | Private tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-r2e-gym) |
| [**cli_gym**](docs/pipelines/env_repair.md) | Restore a damaged development environment | Restoration tests, 0/1 | [25 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-cli-gym) |
| [**seta_seed2synth**](docs/pipelines/terminal_synth.md) | Solve a terminal task synthesized from question/answer seeds | State tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-seta-seed2synth) |
| [**seta_evol**](docs/pipelines/task_evolve.md) | Solve an evolved version of an existing Harbor task | State tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-seta-evol) |
| [**dataarc**](docs/pipelines/dataarc.md) | Solve a related or more demanding variant of a Harbor seed | State tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-dataarc) |
| [**tmax**](docs/pipelines/tmax.md) | Solve a terminal task sampled from a skill taxonomy | State tests, 0/1 | [55 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-tmax) |
| [**endless_terminals**](docs/pipelines/endless_terminals.md) | Solve a task sampled from categories, complexity and scenarios | State tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-endless-terminals) |
| [**terminalworld**](docs/pipelines/terminalworld.md) | Reproduce an outcome reconstructed from a terminal recording | State tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-terminalworld) |
| [**scaler**](docs/pipelines/scaler.md) | Solve a concrete reasoning instance from a problem family | Answer equivalence, −1/+1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-scaler) |

The [release inventory](docs/pipelines/releases.md) distinguishes 50 verified,
5 needing repair and 1,275 unverified tasks across these 15 datasets. The earlier
native datasets are outside those counts. SCALER is a reasoning profile;
DataArc's published version 1 cohort does not measure the replacement prompts
in version 2. SEC-bench remains deferred.

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

**📖 [huggingface.github.io/Repo2RLEnv](https://huggingface.github.io/Repo2RLEnv/)** — the full docs site, built from `docs/` and refreshed on every push to `main`.

Fastest jumps:

- 🚀 [Quickstart](https://huggingface.github.io/Repo2RLEnv/quickstart/) — install → generate → push, in 10 min
- 📦 [Pipelines](https://huggingface.github.io/Repo2RLEnv/pipelines/) — one page per pipeline (status, oracle shape, options, yield)
- 📋 [RFCs](https://huggingface.github.io/Repo2RLEnv/rfcs/) — pipeline designs, shared contracts and implementation status
- 📚 [Reference](https://huggingface.github.io/Repo2RLEnv/reference/API/) — API, SPEC, AUTH, ENV, BOOTSTRAP, AGENTS, REWARD_SCHEMA, RELATED_WORK
- 🛠 [Adding a pipeline](https://huggingface.github.io/Repo2RLEnv/contributing/ADDING_A_PIPELINE/) — cookbook
- 🔭 [Harbor Visualizer](https://huggingface.co/spaces/HuggingFaceH4/harbor-visualiser) — explore any Harbor dataset pushed to the Hub

## Adjacent projects

- [**Harbor**](https://github.com/harbor-framework/harbor) — the task format + runtime we **adopt** as our output spec
- [**RepoLaunch**](https://github.com/microsoft/RepoLaunch) (Microsoft) — LLM-agent env setup; our `bootstrap` is an independent reimplementation
- [**OpenReward**](https://docs.openreward.ai) — ORS protocol + extra trainer integrations above Harbor
- [**SWE-Gym**](https://github.com/SWE-Gym/SWE-Gym) — RL-environment framing for SWE-bench-style tasks
- [**verifiers**](https://github.com/willccbb/verifiers) (Prime Intellect), [**OpenEnv**](https://github.com/meta-pytorch/OpenEnv) (Meta + HF) — adjacent standardization efforts

Each adapted recipe records its source revision, retained material and changes in
`provenance.md`. [Third-party notices](./THIRD_PARTY_NOTICES.md) index the bundled
licenses and credits. See [`docs/reference/RELATED_WORK.md`](./docs/reference/RELATED_WORK.md)
for adjacent papers, datasets and frameworks.

## License

Repo2RLEnv's own code is [Apache 2.0](./LICENSE). Bundled adaptations also retain
MIT and Apache-2.0 material; the distribution declares `Apache-2.0 AND MIT`.
See [third-party notices](./THIRD_PARTY_NOTICES.md) for scope and attribution.
Source repositories, seed data and generated task assets retain their respective
terms. Public availability alone does not establish redistribution permission;
consult each dataset's license and provenance records, including recorded gaps.
