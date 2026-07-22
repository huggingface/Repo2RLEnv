# RFC 0003: `commit_runtime`

**Status:** implemented
**Author:** `@adithya-s-k`
**Created:** 2026-03-01 *(retrospective — pipeline shipped in v0.5.0; RFC written 2026-07-15 as archival record)*

## Summary

R2E-Gym SWE-GEN-style pipeline: walk commit history (not PRs) and turn each qualifying bugfix commit into a sandbox-verified Harbor task. Same F2P/P2P verifier as `pr_runtime`; the only new machinery is the mining stage. Reaches repos and fixes `pr_runtime` cannot — squash-merged / direct-commit repos where the fix lives as a plain commit, not behind a PR merge.

## Motivation

R2E-Gym's headline finding (Jain et al., COLM '25): *"instead of using human-written PRs, good-quality execution environments can directly be curated from commits."* SWE-GEN reaches 34.4% on SWE-Bench Verified with commit-mined data alone. Two concrete motivations for us:

1. **Repos that don't use PRs are invisible to `pr_runtime`.** Solo-maintained crates, research repos, projects that squash-merge everything — all yield 0 from `pr_runtime`, but they have plenty of bugfix commits on `HEAD`. Commit mining unlocks that pool.
2. **Larger candidate pool.** For repos that *do* use PRs, commit history is still 3–10× larger and includes drive-by fixes that never went through review.

The counter-argument: noisier signal per candidate (no reviewer signed off). Our answer: filters at the metadata + structural layer, plus LLM-synthesized instructions (v0.8.4) to compensate for the fact that commit messages are more leak-prone than issue bodies.

## Design

### Input

- **Source** — GitHub · GitLab · local.
- **Trigger** — `repo2rlenv generate --pipeline commit_runtime --repo <owner>/<name> --pipeline-opt limit=120 --pipeline-opt clone_depth=400 --llm anthropic/claude-sonnet-4-6 ...`
- **Options model** — `CommitRuntimeOptions`: mining knobs (`limit`, `since`, `until`, `branch`, `clone_depth`) + metadata filters (`skip_merge_commits`, `min_message_words`, `max_source_files_per_commit`, `exclude_authors`) + structural (`require_new_test_funcs`, `skip_ci_only`) + validation (mirrors `pr_runtime`) + `synthesize_with_llm` + `max_pass_to_pass`.

### Algorithm

1. `git clone --depth N` (default 200; bump for monthly mining).
2. `git log --first-parent` → candidate commits.
3. **Metadata filter**: drop merges, bots, short messages, **non-bugfix conventional-commit types** (`chore:`, `docs:`, `feat:`, `refactor:`, `style:`, `test:`, `ci:`, `build:`, `perf:`, `revert:`), require **bugfix positive signal** (`fix:` prefix OR `Closes #N` OR bugfix keyword in subject).
4. `git show <sha>` → split into `(source_patch, test_patch)` via `pr_runtime._split_patch_and_test_patch`.
5. **Structural filter**: skip CI-only, sweeping refactors, commits without ≥1 new test function.
6. **Validate** inside bootstrap sandbox — reuses `pr_runtime.validate_pr` verbatim.
7. **Instruction** — v0.8.4 default is `synthesize_with_llm=True`: rewrite the commit message into a clean, symptom-focused problem statement with the solution stripped. Fallback path: if `Closes #N`, fetch the linked issue body; else use commit subject + body through `_strip_info_leak` + `_reflow_pr_body`.
8. Emit Harbor task with shared shape.

### Output

Same shape as `pr_runtime` output. `[metadata.repo2env]` adds a `commit_runtime` subtable (`commit_sha`, `parent_sha`, `authored_at`, `author_email`, `subject`, F2P/P2P lists, `validation_status`, `bootstrap_image`).

## Verification

- **Reward kinds** — `test_execution` + `diff_similarity`. Reuses `_pr_runtime_verifier.py`.
- **Oracle invariant** — same as `pr_runtime`: gold patch scores `reward=1.0`.
- **Non-tamper** — same test-file reset + heredoc reapply as `pr_runtime`.

## Anti-contamination

- **Git-history scrub** and **egress guard** from `_env_guard.py` — applied out of the box.
- **Instruction leak-strip** — extended patterns in Arc 3 catch trailing `(#NNNN)` and `repo#N` cross-repo refs.
- **v0.8.4 LLM synthesis** — the biggest leak defense: raw commit messages routinely name the function being fixed ("Fix off-by-one in `Pager.advance`"), and LLM synthesis rewrites them into symptom-only prompts. Audit went 33% → 100% clean.

## LLM use

- **`at bootstrap` (cached)** — reuses `pr_runtime`'s bootstrap. Cache-hit path costs zero.
- **`at synthesis` (per emitted task, v0.8.4+)** — one Sonnet call per task to rewrite the instruction. Cost: ~$0.01–0.03 per task. A 100-env dataset ≈ $1–3 of Sonnet on top of any bootstrap.

## Yield & repo suitability

- **10–35% yield** — same F2P execution gate as `pr_runtime`, applied to raw commits.
- **~0% yield on squash/merge-PR repos** (Pallets/Django/Flask-style): the source change and its test live in a merge commit that the filter correctly rejects. Use `pr_runtime` there.
- **Ideal repos**: direct-commit / squash-merge shops. Go projects, Rust crates, solo-maintained infra. Arc 3's 52-env dataset was Go-heavy for exactly this reason.

## Dependencies

- **`pr_runtime`** for the validation harness (`validate_pr`), eval-script builder (`build_eval_script`), Docker file emitter (`build_environment_dockerfile`), split helper (`_split_patch_and_test_patch`), instruction helpers (`_strip_info_leak`, `_reflow_pr_body`, `_linked_issue_number`), aux-files builder (`_runtime_aux_files`).
- **`bootstrap/`** — shared cache.
- **`github.fetch_issue`** — for the issue-fetch fallback when `Closes #N` present.

## Alternatives considered

- **Fold into `pr_runtime` as a `mine=commits` mode** — rejected. The mining stage is fundamentally different and the option surfaces would collide. Sibling pipeline is cleaner.
- **Continuous commit-mining variant (`commit_stream`)** — explicitly rejected (v0.8.3 removed `pr_stream` as scope-creep; a `commit_stream` would repeat the mistake). If continuous mining ever ships, it's flags on `commit_runtime`, not a separate pipeline.

## Rollout plan

Historic. v0.5.0 shipped; v0.8.3 (Arc 3) tightened filters + issue-fetch fallback + fixed Arc 2 inheritance bug (missing `aux_files`) + published 52-env dataset. v0.8.4 added LLM-synthesized instructions and **promoted from experimental → stable** with the 100-env `-v2` dataset.

## Open questions

Historic — none active.

## References

- R2E-Gym: [arXiv:2504.09724](https://arxiv.org/abs/2504.09724), [R2E-Gym/R2E-Gym](https://github.com/R2E-Gym/R2E-Gym) (Apache-2.0).
- Arc 3 audit: [`docs/release_notes/v0.8.3/findings-commit_runtime.md`](../release_notes/v0.8.3/findings-commit_runtime.md).

## Implementation

| | |
|---|---|
| **Initial PR** | Landed in `ecd31f2` — "Two new pipelines: pr_stream (continuous) + commit_runtime (commit-level)" |
| **Shipping release** | v0.5.0 (experimental); promoted to stable in v0.8.4 |
| **Source file** | [`src/repo2rlenv/pipelines/commit_runtime.py`](https://github.com/huggingface/Repo2RLEnv/blob/mahttps://github.com/huggingface/Repo2RLEnv/blob/main/src/repo2rlenv/pipelines/commit_runtime.py) |
| **Options model** | [`src/repo2rlenv/spec/options.py`](https://github.com/huggingface/Repo2RLEnv/blob/mahttps://github.com/huggingface/Repo2RLEnv/blob/main/src/repo2rlenv/spec/options.py) — `CommitRuntimeOptions` |
| **Doc page** | [`docs/pipelines/commit_runtime.md`](../pipelines/commit_runtime.md) |
| **Findings / release notes** | [`docs/release_notes/v0.8.3/findings-commit_runtime.md`](../release_notes/v0.8.3/findings-commit_runtime.md) |
| **Reference datasets** | [`AdithyaSK/repo2rlenv-commit-runtime`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-commit-runtime) (52 envs, pre-synthesis) · [`AdithyaSK/repo2rlenv-commit-runtime-v2`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-commit-runtime-v2) (100 envs, LLM-synthesized instructions) |
| **Follow-up PRs** | [#47](https://github.com/huggingface/Repo2RLEnv/pull/47) Arc 3 (filters + leak strip + Arc-2 inheritance fix + 52-env dataset) · [#49](https://github.com/huggingface/Repo2RLEnv/pull/49) warn when candidate count reaches clone_depth · [#63](https://github.com/huggingface/Repo2RLEnv/pull/63) local + GitLab source · [#66](https://github.com/huggingface/Repo2RLEnv/pull/66) LLM-synthesized instructions + `max_pass_to_pass` cap · [#67](https://github.com/huggingface/Repo2RLEnv/pull/67) promote to stable + v0.8.4 · [#69](https://github.com/huggingface/Repo2RLEnv/pull/69) anti-contamination · [#75](https://github.com/huggingface/Repo2RLEnv/pull/75) Harbor spec sidecar |
