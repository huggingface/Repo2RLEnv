# Repo2RLEnv

**Turn any repository into an RL environment for training and evaluation.**

> ⚠️ **Experimental.** This is a research project in active development. APIs, spec fields, and CLI flags change between minor versions. Pin to a specific release if you depend on it; expect breaking changes on `main`.

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

# Generate a dataset locally
repo2rlenv generate \
  --repo <owner>/<repo> \
  --pipeline pr_diff \
  --pipeline-opt limit=5 \
  --llm anthropic/claude-sonnet-4-6 \
  --out ./datasets/<dataset-name>

# Or push straight to HF Hub with --out hf://<your-org>/<dataset-name>

# Validate a local dataset against the spec
repo2rlenv validate ./path/to/dataset

# Score a candidate diff against a task's oracle (diff-similarity reward)
repo2rlenv reward --task ./datasets/<dataset-name>/<task-id> --prediction ./candidate.diff

# Or write a sample config first and use --config
repo2rlenv init && repo2rlenv generate --config repo2rlenv.config.yaml
```

Full walkthrough in [**`docs/quickstart.md`**](./docs/quickstart.md).

---

## Pipelines

Different methods to manufacture verifiable tasks from a repo. Pick one, run it, push the dataset.

| Pipeline | Status | Sandbox | Inspiration | Docs |
|---|:-:|:-:|---|:-:|
| `pr_diff` | ✅ | — | [SWE-RL](https://github.com/facebookresearch/swe-rl) | [📄](./docs/pipelines/pr_diff.md) |
| `pr_runtime` | ✅ | ✓ | [SWE-bench](https://github.com/SWE-bench/SWE-bench) | [📄](./docs/pipelines/pr_runtime.md) |
| `pr_stream` | ✅ | ✓ | [SWE-bench-Live](https://github.com/microsoft/SWE-bench-Live) | [📄](./docs/pipelines/pr_stream.md) |
| `commit_runtime` | ✅ | ✓ | [R2E-Gym SWE-GEN](https://github.com/R2E-Gym/R2E-Gym) | [📄](./docs/pipelines/commit_runtime.md) |
| `mutation_bugs` | ✅ | ✓ | [SWE-smith](https://github.com/SWE-bench/SWE-smith) | [📄](./docs/pipelines/mutation_bugs.md) |
| `code_instruct` | ✅ | ✓ | [Magicoder / OSS-Instruct](https://github.com/ise-uiuc/magicoder) | [📄](./docs/pipelines/code_instruct.md) |
| `equivalence_tests` | planned | ✓ | [R2E](https://github.com/r2e-project/r2e) | [📄](./docs/pipelines/equivalence_tests.md) |
| `cve_patches` | planned | ✓ | [PatchSeeker / CVE-Bench](https://github.com/hungkien05/PatchSeeker) | [📄](./docs/pipelines/cve_patches.md) |
| `refactor_synthesis` | planned | ✓ | RefactoringMiner | [📄](./docs/pipelines/refactor_synthesis.md) |

Every pipeline flows through the same QA gate (determinism, oracle consistency, LLM judge, false-negative filter) before tasks are admitted to a dataset. Text-only pipelines skip the heavy QA layers since there's no execution to validate. See [`docs/pipelines/README.md`](./docs/pipelines/README.md) for the full status table including reward kinds + GPU requirements.

---

## Bootstrap (sandbox-required pipelines)

Pipelines marked with a sandbox `✓` above need a working Docker environment for the target repo before they can run. Repo2RLEnv's **bootstrap phase** handles this automatically — an LLM agent iterates shell commands inside a fresh Docker container until the repo builds and the test suite collects. The working image is committed, content-addressed, and cached, so the expensive env-construction step runs **once per (repo, ref)** and every downstream task reuses it. Pure text pipelines (`pr_diff`) skip it entirely.

You don't normally invoke it directly — `repo2rlenv generate --pipeline pr_runtime ...` auto-triggers a cache lookup and runs bootstrap on miss. But you can pre-warm it or use it standalone for debugging:

```bash
repo2rlenv bootstrap \
  --repo <owner>/<repo> \
  --llm anthropic/claude-sonnet-4-6
```

Full design + cache layout + cost-tracking + spec extension fields: [`docs/reference/BOOTSTRAP.md`](./docs/reference/BOOTSTRAP.md).

---

## What you get out

A dataset format that:

- Is **verifiable** — every task carries either an executable test (`test_execution`) or a stored oracle diff (`diff_similarity`); your trainer picks the reward type
- Is **content-addressed** — `content_hash` over each task; same artifacts ⇒ same hash
- **Trains anywhere via Harbor** — TRL, SkyRL, Prime-RL, Tinker, Miles, Slime, harbor.rl
- **Evaluates with any agent harness** — Claude Code, OpenHands, Codex CLI, Gemini CLI, …
- Is **language-agnostic** by spec — `_runtime` pipelines emit Dockerfile + shell verifier; `_diff` pipelines are pure text and work for any language with no extra config
- **Publishes natively** to Hugging Face Hub — `--out hf://owner/name` writes a Harbor-compatible `registry.json` so consumers can `harbor download` without any glue
- Supports **private repos** end-to-end — `gh auth token` resolved automatically; build secrets declared by name; verifier-time secrets forbidden by spec

---

## Under the hood

Repo2RLEnv emits datasets in the [Harbor](https://github.com/harbor-framework/harbor) task format. We don't ship our own sandbox, agent harness, or registry — Harbor already has those. We focus on **synthesis**: turning a real repo into verifiable, reproducible Harbor tasks. A small `[metadata.repo2env]` extension inside Harbor's `task.toml` carries provenance (pipeline name, base commit, PR URL, content hash, reward kinds, etc.).

By targeting Harbor we inherit its full stack: Local Docker / Modal / Daytona / E2B / Runloop sandboxes, every major coding-agent harness, parallel execution, the publishing CLI, and downstream hooks for [OpenReward](https://docs.openreward.ai) (which adds Miles, Slime to the trainer list).

---

## Documentation

Docs are organized into three tiers — see [`docs/README.md`](./docs/README.md) for the index.

- 🚀 [**`docs/quickstart.md`**](./docs/quickstart.md) — install → first dataset → push to Hub, in 10 minutes
- 📖 [**`docs/pipelines/`**](./docs/pipelines/README.md) — one page per synthesis pipeline (status, when to use, oracle shape, inspiration)
- 📚 Reference contracts and module-level API:
  - [`reference/SPEC.md`](./docs/reference/SPEC.md) — input/output contract
  - [`reference/API.md`](./docs/reference/API.md) — Python API for `src/repo2rlenv/`
  - [`reference/AUTH.md`](./docs/reference/AUTH.md) — GitHub / HF / LLM auth resolution
  - [`reference/BOOTSTRAP.md`](./docs/reference/BOOTSTRAP.md) — LLM-iterated per-repo Docker image
  - [`reference/AGENTS.md`](./docs/reference/AGENTS.md) — Harbor agent harnesses + RL trace plumbing
- 🛠 [**`CONTRIBUTING.md`**](./CONTRIBUTING.md) — dev setup, PR conventions, commit style, release flow
- 🧪 [**`contributing/ADDING_A_PIPELINE.md`**](./docs/contributing/ADDING_A_PIPELINE.md) — step-by-step cookbook for shipping a new pipeline

---

## Adjacent projects

Beyond the per-pipeline inspirations linked in the table above, Repo2RLEnv builds on or adjacent to:

- [**Harbor**](https://github.com/harbor-framework/harbor) — the task format + runtime ecosystem we **adopt** as our output spec
- [**RepoLaunch**](https://github.com/microsoft/RepoLaunch) (Microsoft) — LLM-agent-driven environment setup; our `bootstrap` is an independent reimplementation
- [**OpenReward**](https://docs.openreward.ai) — ORS protocol + extra trainer integrations layered above Harbor
- [**SWE-Gym**](https://github.com/SWE-Gym/SWE-Gym) — RL-environment framing for SWE-bench-style tasks
- [**SWE-Bench++**](https://arxiv.org/abs/2512.17419) — four-stage QA pipeline we'll re-implement
- [**verifiers**](https://github.com/willccbb/verifiers) (Prime Intellect), [**OpenEnv**](https://github.com/meta-pytorch/OpenEnv) (Meta + HF) — adjacent standardization efforts

Every pipeline that draws from external work carries an Acknowledgment block in its `.py` file. No code is copied — implementations are independent and licensed Apache-2.0.

---

## Status

Pre-alpha.

- **v0.1** shipped on PyPI: `pr_diff` + HF Hub publish + diff-similarity reward, end-to-end on any GitHub repo (public or private).
- **v0.2** in main: bootstrap phase (LLM-driven Docker env), unified Rich UI, content-addressed cache, registry-qualified pullable digests.
- **v0.3** shipped on PyPI: `pr_runtime` pipeline (sandbox-verified PR mining with `FAIL_TO_PASS` / `PASS_TO_PASS` oracle), auto-triggered bootstrap, structural quality filters, targeted test invocation.
- **v0.4** shipped on PyPI: polyglot log parsers (Go / Cargo / Jest), Harbor end-to-end verification (Mean reward 1.0 on Go via `urfave/cli`).
- **v0.5** shipped on PyPI: `pr_stream` (continuous PR mining, watermark-based) + `commit_runtime` (commit-level mining, SWE-GEN style); defensive git install in emitted Dockerfile so any bootstrap base image works. Harbor-verified on both.
- **v0.6 shipped on PyPI**: first LLM-synthesized pipelines — `mutation_bugs` (AST-based bug injection inspired by SWE-smith) + `code_instruct` (repo-anchored OSS-Instruct inspired by Magicoder, with executable verifiers). Harbor-verified on `pallets/click` (Mean reward 1.000 on both). 271/271 tests passing.
- **v0.7 planned**: LLM-judged QA gate (SWE-Bench++ four-layer recipe) + HF Hub append-mode for `pr_stream` + polyglot mutation (Java/JS/Go via tree-sitter).

## License

Apache 2.0 — see [LICENSE](./LICENSE).
