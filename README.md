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

# Validate the emitted dataset (fast structural check)
repo2rlenv validate ./datasets/<dataset-name>

# Publish to HF Hub
repo2rlenv push ./datasets/<dataset-name> <your-org>/<dataset-name>

# Pull a published dataset back later
repo2rlenv pull <your-org>/<dataset-name> ./datasets/<dataset-name>

# Anyone can pull + run a published dataset on a fresh machine:
harbor download --registry-url https://huggingface.co/datasets/<your-org>/<dataset-name>
harbor run --agent oracle --path ./<dataset-name>/<task-id>
# Note: requires a sandbox-verified pipeline (pr_runtime, commit_runtime, …).
# Text-only pipelines (pr_diff) don't emit an environment/ dir and can't be run here.
```

Full walkthrough in [**`docs/quickstart.md`**](./docs/quickstart.md).

---

## Pipelines

Different methods to manufacture verifiable tasks from a repo. Pick one, run it, push the dataset.

| Pipeline | What it does | Sandbox | LLM | Supported languages | Inspiration | Docs |
|---|---|:-:|:-:|---|---|:-:|
| `pr_diff` | Mine merged PR diffs (text only, no execution) | — | — | any | [SWE-RL](https://github.com/facebookresearch/swe-rl) | [📄](./docs/pipelines/pr_diff.md) |
| `pr_runtime` | Mine merged PRs; sandbox-verify F2P/P2P oracle | ✅ | ✅ | Py · Node · Go · Rust | [SWE-bench](https://github.com/SWE-bench/SWE-bench) | [📄](./docs/pipelines/pr_runtime.md) |
| `pr_stream` | Continuous PR mining (watermark-based, monthly cron) | ✅ | ✅ | Py · Node · Go · Rust | [SWE-bench-Live](https://github.com/microsoft/SWE-bench-Live) | [📄](./docs/pipelines/pr_stream.md) |
| `commit_runtime` | Commit-level mining (bypass PR-review filters) | ✅ | ✅ | Py · Node · Go · Rust | [R2E-Gym SWE-GEN](https://github.com/R2E-Gym/R2E-Gym) | [📄](./docs/pipelines/commit_runtime.md) |
| `mutation_bugs` | Inject bugs via AST mutations; tests must break | ✅ | ✅ | Py only | [SWE-smith](https://github.com/SWE-bench/SWE-smith) | [📄](./docs/pipelines/mutation_bugs.md) |
| `code_instruct` | Repo-anchored OSS-Instruct with executable verifiers | ✅ | ✅ | Py only | [Magicoder / OSS-Instruct](https://github.com/ise-uiuc/magicoder) | [📄](./docs/pipelines/code_instruct.md) |
| `equivalence_tests` | Extract a function; LLM writes equivalence tests | ✅ | ✅ | Py only | [R2E](https://github.com/r2e-project/r2e) | [📄](./docs/pipelines/equivalence_tests.md) |
| `cve_patches` | Map OSV CVEs to fix commits in the target repo | ✅ | ✅ | Py · Node · Go · Rust | [PatchSeeker / CVE-Bench](https://github.com/hungkien05/PatchSeeker) | [📄](./docs/pipelines/cve_patches.md) |
| `refactor_synthesis` | Mine rename refactors from commit history | ✅ | ✅ | Py only | Python-native (drops [RefactoringMiner](https://github.com/tsantalis/RefactoringMiner) JVM dep) | [📄](./docs/pipelines/refactor_synthesis.md) |

Python repos exercise all 9 pipelines; other supported languages exercise the 5 language-agnostic ones. Polyglot mutation + non-Python synthesis are on the v0.9 roadmap.

Every pipeline flows through the same QA gate (determinism, oracle consistency, LLM judge, false-negative filter) before tasks are admitted to a dataset. Text-only pipelines skip the heavy QA layers since there's no execution to validate. See [`docs/pipelines/README.md`](./docs/pipelines/README.md) for reward kinds + GPU requirements.

---

## Bootstrap (sandbox-required pipelines)

Pipelines marked with a sandbox ✅ above need a working Docker environment for the target repo before they can run. Repo2RLEnv's **bootstrap phase** handles this automatically — an LLM agent iterates shell commands inside a fresh Docker container until the repo builds and the test suite collects. The working image is committed, content-addressed, and cached, so the expensive env-construction step runs **once per (repo, ref)** and every downstream task reuses it. Pure text pipelines (`pr_diff`) skip it entirely.

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
- **Publishes natively** to Hugging Face Hub — `repo2rlenv push ./datasets/<name> owner/name` writes a Harbor-compatible `registry.json` so consumers can `harbor download` (or `repo2rlenv pull`) without any glue
- Supports **private repos** end-to-end — `gh auth token` resolved automatically; build secrets declared by name; verifier-time secrets forbidden by spec

---

## Under the hood

Repo2RLEnv emits datasets in the [Harbor](https://github.com/harbor-framework/harbor) task format. We don't ship our own sandbox, agent harness, or registry — Harbor already has those. We focus on **synthesis**: turning a real repo into verifiable, reproducible Harbor tasks. A small `[metadata.repo2env]` extension inside Harbor's `task.toml` carries provenance (pipeline name, base commit, PR URL, content hash, reward kinds, etc.).

By targeting Harbor we inherit its full stack: Local Docker / Modal / Daytona / E2B / Runloop sandboxes, every major coding-agent harness, parallel execution, the publishing CLI, and downstream hooks for [OpenReward](https://docs.openreward.ai) (which adds Miles, Slime to the trainer list).

---

## Documentation

Docs are organized into three tiers — see [`docs/README.md`](./docs/README.md) for the index.

- 🚀 [**`docs/quickstart.md`**](./docs/quickstart.md) — install → first dataset → push to Hub, in 10 minutes
- 📖 [**`docs/pipelines/`**](./docs/pipelines/README.md) — one page per synthesis pipeline (when to use, oracle shape, inspiration)
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

- **v0.1.0** shipped on PyPI: `pr_diff` + HF Hub publish + diff-similarity reward, end-to-end on any GitHub repo (public or private).
- **v0.2**: bootstrap phase (LLM-driven Docker env), unified Rich UI, content-addressed cache, registry-qualified pullable digests. (rolled into v0.3 release)
- **v0.3.0** shipped on PyPI: `pr_runtime` pipeline (sandbox-verified PR mining with `FAIL_TO_PASS` / `PASS_TO_PASS` oracle), auto-triggered bootstrap, structural quality filters, targeted test invocation.
- **v0.4.0** shipped on PyPI: polyglot log parsers (Go / Cargo / Jest), Harbor end-to-end verification (Mean reward 1.0 on Go via `urfave/cli`).
- **v0.5**: `pr_stream` (continuous PR mining, watermark-based) + `commit_runtime` (commit-level mining, SWE-GEN style); defensive git install in emitted Dockerfile so any bootstrap base image works. Harbor-verified on both. (rolled into v0.6 release)
- **v0.6.0** shipped on PyPI: first LLM-synthesized pipelines — `mutation_bugs` (AST-based bug injection inspired by SWE-smith) + `code_instruct` (repo-anchored OSS-Instruct inspired by Magicoder, with executable verifiers). Harbor-verified on `pallets/click` (Mean reward 1.000 on both). 271/271 tests passing.
- **v0.7.0** shipped on PyPI: `equivalence_tests` (R2E-style function-level synthesis — extract a real function, LLM writes equivalence tests, gold patch fills in the candidate with the original) + `cve_patches` (OSV-driven security-fix mining — CVE → fix commit → Harbor task). Harbor-verified on `pallets/click` and `pallets/werkzeug` (Mean reward 1.000 on both).
- **v0.8.0** shipped on PyPI: `refactor_synthesis` (Python-native rename-refactor mining — drop the JVM RefactoringMiner dep; commit-message regex + diff verification + multi-criteria verifier). Harbor-verified on `pallets/click` (Mean reward 1.000). All 8 originally-planned pipelines now shipped.
- **v0.9 planned**: LLM-judged QA gate (SWE-Bench++ four-layer recipe) + iterative refinement for `equivalence_tests` + LLM-synthesized PoC tests for `cve_patches` + HF Hub append-mode for `pr_stream` + polyglot mutation (Java/JS/Go via tree-sitter) + Extract Method / Inline-function refactor kinds for `refactor_synthesis`.

## License

Apache 2.0 — see [LICENSE](./LICENSE).
