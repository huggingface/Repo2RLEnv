---
title: Repo2RLEnv
description: Generate Harbor RL environments from repositories, PRs, task seeds and reasoning families.
hide:
  - navigation
  - toc
---

# Repo2RLEnv

**Generate executable RL environments from repositories, PRs, task seeds and reasoning families.** Each task uses the [Harbor](https://github.com/harbor-framework/harbor) format: an instruction, environment, private verifier and reference solution.

```bash
pip install repo2rlenv

repo2rlenv generate \
    --repo pallets/click \
    --pipeline pr_runtime \
    --pipeline-opt limit=10 \
    --llm anthropic/claude-sonnet-4-6 \
    --out ./datasets/click-pr-runtime
```

<div class="grid cards" markdown>

- :material-rocket-launch:{ .lg .middle } **Quickstart**

    ---

    Install, generate your first dataset, and push it to the Hugging Face Hub — in about 10 minutes.

    [:octicons-arrow-right-24: Start here](quickstart.md)

- :material-source-branch:{ .lg .middle } **Pipelines**

    ---

    Six native pipelines, 15 research-inspired recipes, and Tasksmith — covering repository repair, terminal tasks and reasoning environments.

    [:octicons-arrow-right-24: Browse the pipelines](pipelines/README.md)

- :material-database-outline:{ .lg .middle } **Reference datasets**

    ---

    Browse published Harbor bundles, their generation evidence and evaluation labels. Publication alone does not establish training readiness.

    [:octicons-arrow-right-24: Open the collection](https://huggingface.co/collections/HuggingEnvs/repo2rlenv-verifiable-rl-environments-6aa82300d7494c050f50508d)

- :material-file-document-multiple-outline:{ .lg .middle } **RFCs**

    ---

    Design docs for every pipeline. Read the *why* behind the shape, or draft a new one — the template is in-repo.

    [:octicons-arrow-right-24: Open the RFC index](rfcs/README.md)

</div>

## What's inside

Repo2RLEnv authors and checks tasks; Harbor defines their runtime contract and executes them:

| Layer | Repo2RLEnv ships | We rely on |
|---|---|---|
| **Generation** | Native pipelines, 15 owned recipes, Tasksmith, shared bootstrap and review/repair | — |
| **Spec** | The `[metadata.repo2env]` extension to `task.toml` — source lineage, artifact identities and evaluation labels | [Harbor's task spec](https://www.harborframework.com/docs/tasks) |
| **Consumption** | HF Hub push bridge (`repo2rlenv push`), Harbor-compatible `registry.json` | [Harbor's runtime](https://github.com/harbor-framework/harbor) — sandbox providers and coding-agent integrations |

Tasksmith and the recipes use shared remote execution and budget accounting. The [review and repair loop](pipelines/quality_loop.md) can inspect an existing task, run Harbor controls and learner rollouts, and repair diagnosed issues.

## Pipelines at a glance

Choose a native pipeline, a research-inspired recipe, or Tasksmith's adaptive PR
workflow. **Status describes implementation maturity, not the quality of every
generated task.** The [route guide](pipelines/owned_recipes.md) maps each recipe
to its CLI pipeline family; every linked walkthrough explains its stages and prompts.

### Native pipelines

| Pipeline | Task shape | Reward | Status | Reference dataset |
|---|---|---|:-:|---|
| [**pr_diff**](pipelines/pr_diff.md) | agent writes a patch matching a real PR's diff | 6-component diff-similarity + LLM judge | stable | [`repo2rlenv-pr-diff`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-diff) |
| [**pr_runtime**](pipelines/pr_runtime.md) | SWE-bench-style: agent's patch flips F2P tests to green | graded F2P × P2P | stable | [`repo2rlenv-pr-runtime`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-runtime) |
| [**commit_runtime**](pipelines/commit_runtime.md) | commit-level SWE-Gym-style tasks | graded F2P × P2P | stable | [`repo2rlenv-commit-runtime`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-commit-runtime) |
| [**code_instruct**](pipelines/code_instruct.md) | LLM-authored coding task anchored to a real repo's API | binary `test_execution` | experimental | [`repo2rlenv-code-instruct`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-code-instruct) |
| [**equivalence_tests**](pipelines/equivalence_tests.md) | R2E-style: agent implements a function equivalent to a frozen reference | binary `test_execution` | experimental | [`repo2rlenv-equivalence-tests`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-equivalence-tests) |
| [**cve_patches**](pipelines/cve_patches.md) | OSV-driven CVE → fix-commit as a task | graded F2P × P2P | experimental | [`repo2rlenv-cve-patches`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-cve-patches) |

### Tasksmith — experimental

| Workflow | Task shape | Reward | Reference dataset |
|---|---|---|---|
| [**Tasksmith**](pipelines/tasksmith.md) | Investigate a merged PR, build its environment, design the task, then review and repair it | Private behavioral tests, 0/1 | [HF_ML_Tasksmith · 50 tasks](https://huggingface.co/datasets/HuggingEnvs/HF_ML_Tasksmith) |

Tasksmith uses LangGraph with Pi or OpenCode. Its published cohort contains
50 verified tasks from an **assisted campaign**; Sonnet solved 19. These results
measure that cohort, not unattended success on arbitrary PRs.

### Research-inspired recipes — experimental

All 15 recipes below are implemented. Their stage diagrams, prompts, input
requirements and upstream credits are in the linked guides.

| Recipe | Task shape | Reward | Reference dataset |
|---|---|---|---|
| [**swe_smith**](pipelines/repo_mutate.md) | Repair a deliberately introduced source defect | Private tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-smith) |
| [**swe_gen**](pipelines/pr_to_env.md) | Implement the behavior of a supplied merged PR | Private tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-gen) |
| [**swe_flow**](pipelines/repo_reconstruct.md) | Reconstruct functions in dependency order | Private tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-flow) |
| [**codemidas**](pipelines/codemidas.md) | Reconstruct working functionality from source, with execution-grounded tests and independent rollout review | Private tests, 0/1 | Local campaign; not published |
| [**r2e**](pipelines/r2e.md) | Implement a function equivalent to a private reference | Equivalence tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-r2e) |
| [**swe_next**](pipelines/swe_next.md) | Repair a task mined from PR history | Private tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-next) |
| [**r2e_gym**](pipelines/r2e_gym.md) | Repair a task mined from commit history | Private tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-r2e-gym) |
| [**cli_gym**](pipelines/env_repair.md) | Restore a damaged development environment | Restoration tests, 0/1 | [25 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-cli-gym) |
| [**seta_seed2synth**](pipelines/terminal_synth.md) | Solve a terminal task synthesized from question/answer seeds | State tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-seta-seed2synth) |
| [**seta_evol**](pipelines/task_evolve.md) | Solve an evolved version of an existing Harbor task | State tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-seta-evol) |
| [**dataarc**](pipelines/dataarc.md) | Solve a related or more demanding variant of a Harbor seed | State tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-dataarc) |
| [**tmax**](pipelines/tmax.md) | Solve a terminal task sampled from a skill taxonomy | State tests, 0/1 | [55 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-tmax) |
| [**endless_terminals**](pipelines/endless_terminals.md) | Solve a task sampled from categories, complexity and scenarios | State tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-endless-terminals) |
| [**terminalworld**](pipelines/terminalworld.md) | Reproduce an outcome reconstructed from a terminal recording | State tests, 0/1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-terminalworld) |
| [**scaler**](pipelines/scaler.md) | Solve a concrete reasoning instance from a problem family | Answer equivalence, −1/+1 | [100 tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-scaler) |

SCALER produces reasoning instances rather than repository coding tasks.
DataArc's published cohort used recipe version 1; the current version 2 uses
replacement prompts and still needs a fresh generation-quality pilot.

The [release inventory](pipelines/releases.md) records **1,330 tasks across
15 published Tasksmith/recipe datasets: 50 verified, 5 needing repair and 1,275 unverified**.
The six earlier native datasets and the local CodeMidas campaign are outside these counts. Compare
[yield and cost](pipelines/economics.md), [evaluation labels](pipelines/task_evaluation_labels.md)
and the [complete prompt reference](pipelines/prompt_reference.md).

### Planned or deferred

SEC-bench is deferred. The original native `pr_to_env`, `env_setup`,
`test_synthesis` and `issue_runtime` proposals remain in the [RFC index](rfcs/README.md).
The implemented `pr_to_env / swe_gen` recipe above is a separate, documented route.


## Consuming a dataset

Any dataset from the collection runs end-to-end with `harbor run`:

```bash
uv tool install harbor
repo2rlenv pull AdithyaSK/repo2rlenv-pr-runtime ./workspace/pr-runtime
harbor run --path ./workspace/pr-runtime --agent oracle --env docker
# Every task should score 1.0 with the oracle agent.

harbor run --path ./workspace/pr-runtime --agent claude-code \
    --model anthropic/claude-sonnet-4-6 \
    --sample 10 --backend docker
```

## Links

- **GitHub** — [huggingface/Repo2RLEnv](https://github.com/huggingface/Repo2RLEnv)
- **PyPI** — [`repo2rlenv`](https://pypi.org/project/repo2rlenv/)
- **HF collection** — [Repo2RLEnv — Verifiable RL Environments](https://huggingface.co/collections/HuggingEnvs/repo2rlenv-verifiable-rl-environments-6aa82300d7494c050f50508d)
- **License and credits** — original code under Apache-2.0; retained material and its terms are listed in [third-party notices](https://github.com/huggingface/Repo2RLEnv/blob/main/THIRD_PARTY_NOTICES.md)
