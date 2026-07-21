# RFC 0004: `code_instruct`

**Status:** implemented (experimental)
**Author:** `@adithya-s-k`
**Created:** 2026-04-01 *(retrospective — pipeline shipped in v0.6.0; RFC written 2026-07-15 as archival record)*

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
| **Shipping release** | v0.6.0 (experimental) |
| **Source file** | [`src/repo2rlenv/pipelines/code_instruct.py`](../../src/repo2rlenv/pipelines/code_instruct.py) |
| **Options model** | [`src/repo2rlenv/spec/options.py`](../../src/repo2rlenv/spec/options.py) — `CodeInstructOptions` |
| **Doc page** | [`docs/pipelines/code_instruct.md`](../pipelines/code_instruct.md) |
| **Findings / release notes** | *(none yet — publish alongside the graded-reward + first reference dataset)* |
| **Reference dataset** | *(none yet — bucket B of `plans/todo.md`)* |
| **Follow-up PRs** | [#55](https://github.com/huggingface/Repo2RLEnv/pull/55) fix grading-test unreachable to non-oracle agents · [#63](https://github.com/huggingface/Repo2RLEnv/pull/63) local + GitLab source · [#69](https://github.com/huggingface/Repo2RLEnv/pull/69) anti-contamination · [#75](https://github.com/huggingface/Repo2RLEnv/pull/75) / [#76](https://github.com/huggingface/Repo2RLEnv/pull/76) Harbor spec sidecar |
