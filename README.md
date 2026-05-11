# Repo2RLEnv

**Turn any repository into an RL environment for training and evaluation.**

Repo2RLEnv synthesizes verifiable data from existing repositories using pluggable pipelines, exports it into a uniform spec, and pushes straight to the Hugging Face Hub. End-to-end — **synthesis → standardize → train + eval** — with the focus on training. The uniform spec is [Harbor](https://github.com/harbor-framework/harbor)'s, so the datasets you produce drop straight into any Harbor-compatible runtime.

```
  ╭──────────────╮     ╭──────────────╮     ╭──────────────╮     ╭──────────────────╮
  │     any      │ ──▶ │  synthesize  │ ──▶ │ uniform spec │ ──▶ │ train · eval ·   │
  │     repo     │     │  (pipelines) │     │   (Harbor)   │     │  push to HF Hub  │
  ╰──────────────╯     ╰──────────────╯     ╰──────────────╯     ╰──────────────────╯
                       └──────────────────────── Repo2RLEnv ────────────────────────┘
```

---

## Quickstart

```bash
# Install (pick one)
uv add repo2rlenv                                 # add to a uv-managed project
uvx repo2rlenv --help                             # one-shot, no install
pip install repo2rlenv                            # classic

# Auth: nothing to set up if you've done `gh auth login` and `huggingface-cli login`
# Otherwise:  export GITHUB_TOKEN=... ; export HF_TOKEN=...

# Generate a dataset and push it to the Hub in one shot
repo2rlenv generate \
  --repo huggingface/trl \
  --pipeline pr_mining_lite \
  --pipeline-opt limit=5 \
  --llm anthropic/claude-sonnet-4-6 \
  --out hf://AdithyaSK/trl-r2e-v0-1 --visibility public

# Validate a local dataset against the spec
repo2rlenv validate ./path/to/dataset

# Score a candidate diff against a task's oracle (diff-similarity reward)
repo2rlenv reward --task ./datasets/foo/<task-id> --prediction ./candidate.diff

# Or write a sample config first and use --config
repo2rlenv init && repo2rlenv generate --config repo2rlenv.config.yaml
```

Live example: [`AdithyaSK/trl-r2e-v0-1`](https://huggingface.co/datasets/AdithyaSK/trl-r2e-v0-1) — 5 PRs from `huggingface/trl`, served via Harbor's `registry.json` format.

---

## Pipelines

Different methods to manufacture verifiable tasks from a repo. Pick one, run it, push the dataset.

| Pipeline | Status | Inspiration |
|---|---|---|
| [`pr_mining_lite`](./docs/pipelines/pr_mining_lite.md) | **shipped (v0.1)** | [SWE-RL](https://github.com/facebookresearch/swe-rl) |
| [`pr_mining`](./docs/pipelines/pr_mining.md) | planned | [SWE-bench](https://github.com/SWE-bench/SWE-bench) |
| [`commit_mining`](./docs/pipelines/commit_mining.md) | planned | [R2E-Gym SWE-GEN](https://github.com/R2E-Gym/R2E-Gym) |
| [`mutation`](./docs/pipelines/mutation.md) | planned | [SWE-smith](https://github.com/SWE-bench/SWE-smith) |
| [`oss_instruct`](./docs/pipelines/oss_instruct.md) | planned | [Magicoder](https://github.com/ise-uiuc/magicoder) |
| [`equivalence_tests`](./docs/pipelines/equivalence_tests.md) | planned | [R2E](https://github.com/r2e-project/r2e) |
| [`live_pr_mining`](./docs/pipelines/live_pr_mining.md) | planned | [SWE-bench-Live](https://github.com/microsoft/SWE-bench-Live) |
| [`cve_mining`](./docs/pipelines/cve_mining.md) | planned (v1.0) | [PatchSeeker](https://github.com/hungkien05/PatchSeeker) |
| [`refactor_synthesis`](./docs/pipelines/refactor_synthesis.md) | planned (v1.0) | RefactoringMiner |

Each shipped pipeline is text-only or sandbox-required; all of them flow through the same QA gate (determinism, oracle consistency, LLM judge, false-negative filter) before tasks are admitted to a dataset. The lite path skips the heavy QA layers since there's no execution to validate.

---

## What you get out

A dataset format that:

- Is **verifiable** — every task carries either an executable test (`test_execution`) or a stored oracle diff (`diff_similarity`) — your trainer picks the reward type
- Is **content-addressed** — `content_hash` over each task; same artifacts ⇒ same hash
- **Trains anywhere via Harbor** — TRL, SkyRL, Prime-RL, Tinker, Miles, Slime, harbor.rl
- **Evaluates with any agent harness** — Claude Code, OpenHands, Codex CLI, Gemini CLI
- Is **language-agnostic** by spec — full pipelines emit Dockerfile + shell verifier; lite pipelines are pure text and work for any language with no extra config
- **Publishes natively** to Hugging Face Hub — `--out hf://owner/name` writes a Harbor-compatible `registry.json` so consumers can `harbor download` without any glue
- Supports **private repos** end-to-end — `gh auth token` resolved automatically; build secrets declared by name; verifier-time secrets forbidden by spec

---

## Under the hood

Repo2RLEnv emits datasets in the [Harbor](https://github.com/harbor-framework/harbor) task format. We don't ship our own sandbox, agent harness, or registry — Harbor already has those. We focus on **synthesis**: turning a real repo into verifiable, reproducible Harbor tasks. A small `[metadata.repo2env]` extension inside Harbor's `task.toml` carries provenance (pipeline name, base commit, PR URL, content hash, reward kinds, etc.).

By targeting Harbor we inherit its full stack: Local Docker / Modal / Daytona / E2B / Runloop sandboxes, every major coding-agent harness, parallel execution, the publishing CLI, and downstream hooks for [OpenReward](https://docs.openreward.ai) (which adds Miles, Slime to the trainer list).

---

## Documentation

- [`docs/SPEC.md`](./docs/SPEC.md) — input contract (`GenerationInput`) + Harbor-shaped output contract
- [`docs/AUTH.md`](./docs/AUTH.md) — GitHub auth (PAT / `gh` CLI / per-repo env), HF Hub, LLM keys
- [`docs/API.md`](./docs/API.md) — Python API reference for the modules in `src/repo2rlenv/`
- [`docs/pipelines/`](./docs/pipelines/) — per-pipeline docs with Mermaid flowcharts

---

## Status

Pre-alpha. `pr_mining_lite` + HF Hub push works end-to-end on any GitHub repo, public or private. 26 unit + e2e tests passing against `huggingface/trl` and `huggingface/trl-internal`.

Next on the roadmap: `pr_mining` (full sandbox-required), `mutation`, `oss_instruct`. See [the pipelines index](./docs/pipelines/) for status of each.

## Credits

Repo2RLEnv stands on shoulders:

- [Harbor](https://github.com/harbor-framework/harbor) — task format and runtime ecosystem we adopt
- [OpenReward](https://docs.openreward.ai) — ORS protocol + extra trainer integrations layered above Harbor
- [SWE-bench](https://github.com/SWE-bench/SWE-bench) / [SWE-bench Verified](https://openai.com/index/introducing-swe-bench-verified/) — original PR-as-task formulation
- [SWE-RL](https://github.com/facebookresearch/swe-rl) — diff-similarity reward concept that powers `pr_mining_lite`
- [SWE-Bench++](https://arxiv.org/abs/2512.17419) — four-stage QA pipeline we'll re-implement
- [SWE-smith](https://github.com/SWE-bench/SWE-smith) — mutation-based synthesis
- [R2E](https://github.com/r2e-project/r2e), [R2E-Gym](https://github.com/R2E-Gym/R2E-Gym), [SWE-bench-Live](https://github.com/microsoft/SWE-bench-Live), [Magicoder](https://github.com/ise-uiuc/magicoder) — referenced for upcoming pipelines
- [verifiers](https://github.com/willccbb/verifiers) (Prime Intellect), [OpenEnv](https://github.com/meta-pytorch/OpenEnv) (Meta + HF) — adjacent standardization efforts

## License

Apache 2.0 — see [LICENSE](./LICENSE).
