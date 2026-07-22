---
title: Repo2RLEnv
description: Turn any GitHub repository into a verifiable RL training and evaluation environment.
hide:
  - navigation
  - toc
---

# Repo2RLEnv

**Turn any GitHub repository into a verifiable RL training and evaluation dataset.** End-to-end synthesis → standardize → train + eval, in the [Harbor](https://github.com/harbor-framework/harbor) task format.

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

    Six synthesis pipelines out of the box — from PR mining to LLM-authored coding tasks — each with a graded, verifiable reward.

    [:octicons-arrow-right-24: Browse the pipelines](pipelines/README.md)

- :material-database-outline:{ .lg .middle } **Reference datasets**

    ---

    Every pipeline ships with a published HF dataset that you can pull, benchmark against, or use as-is for training.

    [:octicons-arrow-right-24: Open the collection](https://huggingface.co/collections/AdithyaSK/repo2rlenv-verifiable-rl-environments-6a15e7eee7c112fe841b2990)

- :material-file-document-multiple-outline:{ .lg .middle } **RFCs**

    ---

    Design docs for every pipeline. Read the *why* behind the shape, or draft a new one — the template is in-repo.

    [:octicons-arrow-right-24: Open the RFC index](rfcs/README.md)

</div>

## What's inside

Three layers — the first is ours, the other two we delegate to Harbor:

| Layer | Repo2RLEnv ships | We rely on |
|---|---|---|
| **Generation** | `src/repo2rlenv/pipelines/` — six pipelines, gate helpers, quality audits | — |
| **Spec** | The `[metadata.repo2env]` extension to `task.toml` — provenance so a dataset can be regenerated exactly | [Harbor's task spec](https://www.harborframework.com/docs/tasks) |
| **Consumption** | HF Hub push bridge (`repo2rlenv push`), Harbor-compatible `registry.json` | [Harbor's full runtime](https://github.com/harbor-framework/harbor) — 5 sandbox backends × 22 agent harnesses |

Repo2RLEnv is **synthesis-only** — we generate the datasets and let Harbor run them. No parallel sandbox runtime; no bespoke evaluation harness.

## Pipelines at a glance

| Pipeline | Task shape | Reward | Status | Reference dataset |
|---|---|---|:-:|---|
| [**pr_diff**](pipelines/pr_diff.md) | agent writes a patch matching a real PR's diff | 6-component diff-similarity + LLM judge | stable | [`repo2rlenv-pr-diff`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-diff) |
| [**pr_runtime**](pipelines/pr_runtime.md) | SWE-bench-style: agent's patch flips F2P tests to green | graded F2P × P2P | stable | [`repo2rlenv-pr-runtime`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-runtime) |
| [**commit_runtime**](pipelines/commit_runtime.md) | commit-level SWE-Gym-style tasks | graded F2P × P2P | stable | [`repo2rlenv-commit-runtime`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-commit-runtime) |
| [**code_instruct**](pipelines/code_instruct.md) | LLM-authored coding task anchored to a real repo's API | binary `test_execution` | experimental | [`repo2rlenv-code-instruct`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-code-instruct) |
| [**equivalence_tests**](pipelines/equivalence_tests.md) | R2E-style: agent implements a function equivalent to a frozen reference | binary `test_execution` | experimental | [`repo2rlenv-equivalence-tests`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-equivalence-tests) |
| [**cve_patches**](pipelines/cve_patches.md) | OSV-driven CVE → fix-commit as a task | graded F2P × P2P | experimental | [`repo2rlenv-cve-patches`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-cve-patches) |

Four more pipelines in the RFC queue: [`pr_to_env`](rfcs/0007-pr-to-env.md) · [`env_setup`](rfcs/0008-env-setup.md) · [`test_synthesis`](rfcs/0009-test-synthesis.md) · [`issue_runtime`](rfcs/0010-issue-runtime.md).

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
- **HF collection** — [Repo2RLEnv — Verifiable RL Environments](https://huggingface.co/collections/AdithyaSK/repo2rlenv-verifiable-rl-environments-6a15e7eee7c112fe841b2990)
- **License** — Apache-2.0
