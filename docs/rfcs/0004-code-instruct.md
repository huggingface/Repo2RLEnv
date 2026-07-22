# RFC 0004: `code_instruct`

**Status:** implemented (experimental) — hardened v0.8.6 with repo-anchoring gate + delivery contract
**Author:** `@adithya-s-k`
**Created:** 2026-04-01 *(retrospective — pipeline shipped in v0.6.0; RFC written 2026-07-15 as archival record; updated 2026-07-21 for v0.8.6 self-improvement pass)*

## Summary

Magicoder OSS-Instruct, grounded in a specific target repo and **verified by execution**. The LLM proposes a coding task seeded by a real snippet from the repo's actual code and writes both an executable test and a candidate solution. The pipeline runs the synthesized test inside the repo's bootstrap container to confirm it FAILS without the oracle and PASSES with it. Emits a Harbor task whose reward is `test_execution` on the LLM-authored test.

## Motivation

Two synthesis approaches were floated for v0.6:
- **`mutation_bugs`** — inject synthetic AST bugs. Cheap, deterministic, but the bugs are unrealistic.
- **`code_instruct`** — LLM proposes tasks anchored to real repo code. Costs an LLM call per candidate, produces realistic problems.

Both shipped in v0.6.0. `mutation_bugs` was removed in v0.9 (audit — synthetic AST bugs turned out to be too unrealistic for RL signal). `code_instruct` survived because its tasks are grounded in the target repo's actual code — the LLM invents a problem that reads like something a junior engineer would write, and if the executable-verifier gate passes, we get a synthesized-but-real task.

The key differentiator vs. Magicoder itself: **each task ships an executable verifier**, not just a `(problem, solution)` text pair. That's the RL signal we care about — verifiable execution, not text similarity.

## Design

### Input

- **Source** — GitHub · GitLab · local.
- **Trigger** — `repo2rlenv generate --pipeline code_instruct --repo <owner>/<name> --pipeline-opt limit=50 --llm anthropic/claude-sonnet-4-6 ...`
- **Options model** — `CodeInstructOptions`: `limit`, `max_attempts_per_seed`, `seed_min_loc`, `seed_max_loc`, `min_snippet_words`, generation LLM knobs. Python-only via `supported_languages`.

### Algorithm

1. Enumerate Python source files in the target repo.
2. Sample seed snippets (LOC-bounded) from those files.
3. For each seed: LLM proposes `(problem, executable_test, candidate_solution)` in a single call.
4. **Execute both sides in the bootstrap sandbox**:
   - Test on `base_commit` (no candidate applied) → must FAIL.
   - Test with candidate applied → must PASS.
5. Emit Harbor task if both gates pass.

### Output

- Task shape: standard `pr_runtime`-style with the LLM-authored test in the test.sh heredoc, and the LLM's candidate solution in `solution/patch.diff` (the "oracle").
- `[metadata.repo2env]` provenance: `code_instruct` subtable with seed file path, seed LOC range, generation LLM.

## Verification

- **Reward kind** — `test_execution`. Binary today (`reward = 1.0` if the LLM-authored test passes on the agent's patch, else `0.0`).
- **Oracle invariant** — the candidate solution the LLM wrote passes its own test at emit time. Enforced by the gate above.
- **Non-tamper** — same test-file reset + heredoc reapply as `pr_runtime`.

**Known limitation**: reward is binary. Bringing this onto the shared graded machinery is a **v0.9 roadmap item** (`todo.md` bucket B).

## v0.8.6 self-improvement pass (2026-07-21)

The reference-dataset run flagged two failure modes that broke ~60% of trials without touching model quality:

1. **Repo-anchoring collapse.** The v0.6 system prompt explicitly told the LLM to design a task *"that does NOT require any of the repo's APIs"* — the opposite of the RL goal. Baseline audit on 20 envs across 5 repos: mean repo-anchoring score **1.40 / 5**, zero of 20 imported the target package. Fixed by rewriting the synthesis prompt to demand a `from <pkg> import ...` in the oracle plus AST-level gates (`check_repo_anchoring`, `check_symbol_collision`, `check_test_strength`, `task_fingerprints`) in `_oss_instruct.py`. Retries wired via `max_attempts_per_seed` (default bumped 1 → 3). Post-fix audit: **RA 4.95 / TR 4.95 / OP 5.00 / RH 4.40**, zero scores ≤ 2.
2. **Missing delivery contract in the emitted instruction.** All three real agents (Sonnet, GPT-5.3-Codex, Qwen3.6-35B) failed the exact same 3/5 sample tasks with `ModuleNotFoundError: No module named 'task_module'` at pytest collection. Every model correctly implemented the requested logic and wrote it to natural filenames (`ranged_float.py`, `fetcher.py`). Fix: append a delivery-contract paragraph to the emitted `instruction.md` at `_build_task` time. Solve rate on the previously-failing 3 tasks jumped **0/3 → 2/3** (Sonnet, `claude-code`), extrapolated matrix-solve rate **40% → 80%**.

Both changes ride under `pipeline_version = "0.6.2"` in `[metadata.repo2env]`, so downstream consumers can distinguish pre-fix and post-fix tasks. All 5 iter1 gate helpers ship as tests in `tests/test_oss_instruct_gates.py`.

## Anchoring gate details

The gate layer in `_oss_instruct.py`:

- **`detect_repo_package`** — resolves the target Python package name from `pyproject.toml` (with a fallback to standard `src/<name>` / `<name>` layouts). Special-cases `attrs` → `attr`.
- **`list_repo_top_level_symbols`** — AST-walks the repo's package for all top-level class/def names. Used by the collision guard.
- **`check_repo_anchoring`** — AST scan of the LLM-emitted oracle: at least one `Import`/`ImportFrom` naming the target package, and at least one imported name that appears outside its import line (rejects the "bare `import X`, never referenced" failure mode).
- **`check_symbol_collision`** — reject candidates whose top-level class/def names are already in the repo (blocks `grep`-and-re-export cheats).
- **`check_test_strength`** — reject weak tests: `<3` non-trivial `assert` statements, presence of `assert True` / literals / `x == x`, or missing `pytest.raises` on instructions that mention "raise / error / invalid".
- **`task_fingerprints`** — dedup by problem-head (first 80 normalized chars) *and* by sorted public top-level symbol names. Overlap on either counts as duplicate (catches reworded-but-same-class variants like `RangedFloatType` shipped as both a `click.Command` and a `click.Option` binding).

The synthesis prompt now names the target package explicitly and demands the oracle build on its public API. Failed candidates loop back through the retry knob rather than skipping the seed.

## Anti-contamination

- **Git-history scrub** + **egress guard** — applied out of the box.
- **Instruction leak concern** — the LLM writes the problem statement itself, so the "leak" is whatever the LLM chooses to include. Prompt engineering keeps it symptom-focused. Empirically clean, but not audit-proven at the level of `pr_runtime` or v0.8.4 `commit_runtime`.
- **Reachability fix in #55**: pre-fix, the LLM-authored test file wasn't reachable to non-oracle agents (they ran the repo's original tests, not the emitted one). Fixed.

## LLM use

- **`at bootstrap` (cached)** — reuses `pr_runtime`'s bootstrap.
- **`at synthesis` (per emitted task)** — 1 call per seed × `max_attempts_per_seed` attempts. Realistic budget: ~$0.02–0.10 per emitted task (some seeds need retries).
- **Cost for 100 envs** — ~$5–15 of Sonnet on top of any bootstrap.

## Yield & repo suitability

- **40–70% yield** — fraction of seeds where the LLM's test fails-without / passes-with the oracle in the container.
- **Python-only** — the LLM prompt + test scaffolding are pytest-shaped. Polyglot support is on the v1.0 roadmap.
- **Best on repos with clean, self-contained modules** (utility libs). Struggles on framework-heavy repos where a seed can't be tested in isolation.

## Dependencies

- **`bootstrap/`** — for the Docker env.
- **`_pr_runtime_verifier.py`** — reused for the binary reward; upgrade to graded is pending.
- **`_env_guard.py`** — anti-contamination.
- LiteLLM for the synthesis call.

## Alternatives considered

- **Text-only Magicoder** — rejected. No RL signal.
- **Seed from the repo's tests** (instead of source) — dropped in early prototyping; the LLM writes better problems when seeded with the code it's supposed to exercise.

## Rollout plan

Historic. v0.6.0 shipped experimental. Ongoing work: graded reward port (v0.9 roadmap), reference dataset publish.

## Open questions

- **When does this graduate from experimental?** Blocker: graded reward + published reference dataset. Both on the v0.9 roadmap.
- **Class-method support** — currently module-level Python only. Class methods with `self` / `cls` deferred (same limitation as `equivalence_tests`).

## References

- Magicoder: [ICML '24](https://arxiv.org/abs/2312.02120), [ise-uiuc/magicoder](https://github.com/ise-uiuc/magicoder).

## Implementation

| | |
|---|---|
| **Initial PR** | [#8](https://github.com/huggingface/Repo2RLEnv/pull/8) — v0.6: mutation_bugs + code_instruct (first LLM-synthesized pipelines) |
| **Shipping release** | v0.6.0 (experimental); hardened v0.8.6 (repo-anchoring + delivery contract + reference dataset) |
| **Source file** | [`src/repo2rlenv/pipelines/code_instruct.py`](https://github.com/huggingface/Repo2RLEnv/blob/mahttps://github.com/huggingface/Repo2RLEnv/blob/main/src/repo2rlenv/pipelines/code_instruct.py) · [`_oss_instruct.py`](https://github.com/huggingface/Repo2RLEnv/blob/mahttps://github.com/huggingface/Repo2RLEnv/blob/main/src/repo2rlenv/pipelines/_oss_instruct.py) (gate helpers) |
| **Options model** | [`src/repo2rlenv/spec/options.py`](https://github.com/huggingface/Repo2RLEnv/blob/mahttps://github.com/huggingface/Repo2RLEnv/blob/main/src/repo2rlenv/spec/options.py) — `CodeInstructOptions` |
| **Doc page** | [`docs/pipelines/code_instruct.md`](../pipelines/code_instruct.md) |
| **Findings / release notes** | v0.8.6 self-improvement writeup: `plans/code_instruct_audit_iter0.md`, `plans/code_instruct_audit_iter1.md`, `plans/code_instruct_audit_failure_modes.md` (gitignored working docs) |
| **Reference dataset** | [`AdithyaSK/repo2rlenv-code-instruct`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-code-instruct) — 100 tasks across click, flask, requests, attrs, starlette (Sonnet-generated, oracle-verified, sample-validated 4/5 on Sonnet-solve) |
| **Follow-up PRs** | [#55](https://github.com/huggingface/Repo2RLEnv/pull/55) fix grading-test unreachable to non-oracle agents · [#63](https://github.com/huggingface/Repo2RLEnv/pull/63) local + GitLab source · [#69](https://github.com/huggingface/Repo2RLEnv/pull/69) anti-contamination · [#75](https://github.com/huggingface/Repo2RLEnv/pull/75) / [#76](https://github.com/huggingface/Repo2RLEnv/pull/76) Harbor spec sidecar |
