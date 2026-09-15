<h1 align="center">Repo2RLEnv</h1>
<p align="center"><b>Turn repositories, pull requests and task seeds into executable RL environments.</b></p>

<p align="center">
  <a href="https://pypi.org/project/repo2rlenv/"><img alt="PyPI" src="https://img.shields.io/pypi/v/repo2rlenv?color=blue"></a>
  <a href="https://pypi.org/project/repo2rlenv/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/repo2rlenv"></a>
  <a href="https://github.com/huggingface/Repo2RLEnv/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/huggingface/Repo2RLEnv/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/huggingface/Repo2RLEnv/blob/main/THIRD_PARTY_NOTICES.md"><img alt="License: Apache-2.0 and MIT" src="https://img.shields.io/badge/license-Apache--2.0%20AND%20MIT-green"></a>
  <a href="https://harborframework.com/"><img alt="Harbor task format" src="https://img.shields.io/badge/spec-Harbor-FFD21F"></a>
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> ·
  <a href="#pipelines">Pipelines</a> ·
  <a href="#harbor-output">Output</a> ·
  <a href="#datasets-and-quality">Datasets</a> ·
  <a href="https://huggingface.github.io/Repo2RLEnv/">Documentation</a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/huggingface/Repo2RLEnv/main/assets/banner.png" alt="Repo2RLEnv — generate, verify and share RL environments" width="100%">
</p>

Repo2RLEnv generates coding, terminal and reasoning tasks in the
[Harbor format](https://github.com/harbor-framework/harbor): a task instruction,
starting environment, reference solution and executable verifier. Run them with
Harbor agents, inspect their quality, and publish them to the Hugging Face Hub.

```text
Repository / PR / task seed
            │
            ▼
 Native pipeline · Tasksmith · Research recipe
            │
            ▼
 Harbor task: instruction + environment + solution + verifier
            │
            ▼
 Static checks → baseline / oracle → review and learner rollout
            │                          ↺ bounded repair
            ▼
 Labeled tasks + evidence → Hugging Face Hub → training / evaluation
```

Generation methods have different checks. The shared quality workflow can review
and repair emitted tasks; exporting a task alone does not establish its quality.

## Quickstart

Requires **Python 3.12+** and Git. This example generates PR-diff tasks without
building a container:

```bash
pip install repo2rlenv

# GitHub access; alternatively set GITHUB_TOKEN
gh auth login

repo2rlenv generate \
  --repo pallets/click --pipeline pr_diff \
  --pipeline-opt limit=3 --out ./workspace/click-tasks

repo2rlenv validate ./workspace/click-tasks --deep
repo2rlenv pipelines list
```

For test-based repository tasks, use `pr_runtime` or Tasksmith. LLM stages need a
provider key such as `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. Native runtime
pipelines build and cache Docker environments; owned recipes and Tasksmith build
and execute remotely through Daytona or Modal.

Install the extras your route needs:

```bash
pip install 'repo2rlenv[tasksmith,daytona,harbor]'
# Other extras: modal, mutation
repo2rlenv tasksmith install-runtime  # Pi / OpenCode; requires Node.js 22.19+
```

See the [quickstart](https://huggingface.github.io/Repo2RLEnv/quickstart/) for
execution and publishing, and [authentication](https://huggingface.github.io/Repo2RLEnv/reference/AUTH/)
for model, sandbox and Hub credentials.

## Pipelines

### Native pipelines

| Pipeline | Task |
|---|---|
| [`pr_diff`](https://huggingface.github.io/Repo2RLEnv/pipelines/pr_diff/) | Reproduce a real PR change; scored by diff similarity and an optional LLM judge |
| [`pr_runtime`](https://huggingface.github.io/Repo2RLEnv/pipelines/pr_runtime/) | Fix a PR regression; failing tests must pass while existing tests stay green |
| [`commit_runtime`](https://huggingface.github.io/Repo2RLEnv/pipelines/commit_runtime/) | Build the same test-based task from commit history |
| [`code_instruct`](https://huggingface.github.io/Repo2RLEnv/pipelines/code_instruct/) | Solve an LLM-authored problem grounded in repository APIs |
| [`equivalence_tests`](https://huggingface.github.io/Repo2RLEnv/pipelines/equivalence_tests/) | Implement a function matching a private reference |
| [`cve_patches`](https://huggingface.github.io/Repo2RLEnv/pipelines/cve_patches/) | Repair a vulnerability identified through public CVE and fix-commit records |

The first three are marked stable; the others are experimental. Supported source
hosts and languages vary by pipeline. See the [pipeline guide](https://huggingface.github.io/Repo2RLEnv/pipelines/).

### Tasksmith

[Tasksmith](https://huggingface.github.io/Repo2RLEnv/pipelines/tasksmith/) adaptively
converts a merged PR into a Harbor task. **LangGraph orchestrates Pi or OpenCode**
to investigate the change, establish a working environment, and design the
instruction and private verifier. The merged implementation supplies the oracle.
Baseline checks, review, learner rollouts and bounded repair produce labeled
revisions with execution evidence and cost records.

Tasksmith is experimental and currently targets testable Python changes.
CPU execution supports **Daytona and Modal**; the implemented GPU route uses
**Modal L4 GPUs**. Start with the [one-PR walkthrough](https://huggingface.github.io/Repo2RLEnv/pipelines/tasksmith/#run-one-pr),
which includes example inputs, provider setup and an explicit spending limit.

### Research recipes

These **14 experimental recipes** adapt published methods into code owned by this
repository. No upstream research package is installed at runtime. A pipeline
names the generation family; a recipe selects its method.

| Recipe | Pipeline family | Task |
|---|---|---|
| `swe_smith` | `repo_mutate` | Repair an introduced source defect |
| `swe_gen` | `pr_to_env` | Implement a supplied merged PR's behavior |
| `swe_next` | `pr_runtime` | Repair a task mined from PR history |
| `r2e_gym` | `commit_runtime` | Repair a task mined from commit history |
| `swe_flow` | `repo_reconstruct` | Reconstruct functions in dependency order |
| `r2e` | `equivalence_tests` | Match a private reference through generated tests |
| `cli_gym` | `env_repair` | Restore a damaged development environment |
| `seta_seed2synth` | `terminal_synth` | Solve a terminal task derived from question/answer seeds |
| `seta_evol` | `task_evolve` | Solve an evolved Harbor task |
| `dataarc` | `terminal_synth` | Solve a related or harder variant of a Harbor seed |
| `tmax` | `terminal_synth` | Solve a task sampled from a skill taxonomy |
| `endless_terminals` | `terminal_synth` | Solve a task sampled from categories and scenarios |
| `terminalworld` | `terminal_reconstruct` | Reproduce an outcome from a terminal recording |
| `scaler` | `reasoning_synth` | Solve a reasoning instance generated from a problem family |

```bash
repo2rlenv pipelines describe repo_mutate --recipe swe_smith --json
```

Each [recipe walkthrough](https://huggingface.github.io/Repo2RLEnv/pipelines/owned_recipes/)
includes configuration, a pipeline diagram, model prompts, verification steps,
limitations and upstream credits. Use its example config with
`repo2rlenv generate --config <config.yaml>`.

## Harbor output

```text
<task-id>/
├── instruction.md          # Learner's task
├── task.toml               # Runtime, resources, provenance and evaluation labels
├── environment/            # Dockerfile, source snapshot and fixtures
├── solution/solve.sh        # Private reference entrypoint
└── tests/test.sh            # Trusted verifier entrypoint
```

Private solutions and tests belong to their respective execution phases; the
whole task bundle is not the learner workspace. Task assets and reference formats
vary by recipe. Most owned verifiers return deterministic 0/1 rewards; SCALER uses
−1/+1, and native pipelines also support graded test and diff-similarity rewards.

With the `harbor` extra and a configured runtime, execute a reference solution:

```bash
harbor run -p ./workspace/click-tasks -a oracle --env docker
```

`validate --deep` checks files and metadata without execution. Use
[review and repair](https://huggingface.github.io/Repo2RLEnv/pipelines/quality_loop/)
for instruction quality, verifier defects, leakage and rollout evidence.

## Datasets and quality

Explore the [HuggingEnvs collection](https://huggingface.co/collections/HuggingEnvs/repo2rlenv-verifiable-rl-environments-6aa82300d7494c050f50508d)
with the [Harbor Visualizer](https://huggingface.co/spaces/HuggingFaceH4/harbor-visualiser).
The [release inventory](https://huggingface.github.io/Repo2RLEnv/pipelines/releases/)
records per-pipeline task counts, revisions and validation scope; the
[economics guide](https://huggingface.github.io/Repo2RLEnv/pipelines/economics/)
reports measured yield and cost per task.

The Tasksmith reference cohort contains **50 verified tasks from an assisted
campaign**. Research-recipe exports retain separate quality labels, including
unverified and needs-repair. Consult the recorded evidence before using a cohort
for training or evaluation.

```bash
hf auth login  # Or set HF_TOKEN with write access to your namespace
repo2rlenv push ./workspace/click-tasks <your-org>/<dataset-name>
repo2rlenv pull <your-org>/<dataset-name> ./workspace/downloaded-tasks
```

## Documentation and contributing

[Documentation](https://huggingface.github.io/Repo2RLEnv/) ·
[Exact prompts](https://huggingface.github.io/Repo2RLEnv/pipelines/prompt_reference/) ·
[Design RFCs](https://huggingface.github.io/Repo2RLEnv/rfcs/) ·
[Contributing](https://github.com/huggingface/Repo2RLEnv/blob/main/CONTRIBUTING.md) ·
[Adding a pipeline](https://huggingface.github.io/Repo2RLEnv/contributing/ADDING_A_PIPELINE/) ·
[Release notes](https://huggingface.github.io/Repo2RLEnv/release_notes/HISTORY/)

## License and credits

Repo2RLEnv's own code is [Apache-2.0](https://github.com/huggingface/Repo2RLEnv/blob/main/LICENSE).
Bundled adaptations retain their MIT and Apache-2.0 licenses; the distribution is
`Apache-2.0 AND MIT`. [Third-party notices](https://github.com/huggingface/Repo2RLEnv/blob/main/THIRD_PARTY_NOTICES.md)
link each recipe's source revision, retained material and attribution.
Source repositories and generated task assets retain their respective terms;
check each dataset's license and provenance before redistribution.
