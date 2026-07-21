# RFC 0001: `pr_diff`

**Status:** implemented
**Author:** `@adithya-s-k`
**Created:** 2026-01-15 *(retrospective — pipeline shipped in v0.1.0; RFC written 2026-07-15 as archival record)*

## Summary

Mine merged pull-request diffs from a target repo into text-only Harbor tasks with a 6-component graded reward (5 deterministic components + 1 LLM-as-judge). No per-repo Docker bootstrap; every task ships a thin `python:3.12-slim` image with the repo cloned at the PR's `base_commit` and the oracle + verifier base64-baked in. The lowest-cost pipeline in the set.

## Motivation

The starting point for the project. Datasets of merged PR diffs are the closest thing there is to a natural corpus of "here's a code change plus the human review signal that it was correct." SWE-RL (Meta FAIR, arXiv:2502.18449) showed that a diff-similarity reward on merged PRs is enough to bootstrap a coding-agent RL loop without any executable tests. We wanted that shape in Repo2RLEnv as the *baseline* pipeline: cheap to generate, no bootstrap dependency, works on any repo with merged PRs. Every subsequent pipeline is compared against it for cost/signal ratio.

## Design

### Input

- **Source** — GitHub · GitLab (via input-source abstraction added in v0.8.4).
- **Trigger** — `repo2rlenv generate --pipeline pr_diff --repo <owner>/<name> --pipeline-opt limit=100 ...`
- **Options model** — `PrDiffOptions`: `limit`, `since`, `until`, `skip_drafts`, `min_loc_changed`, `max_files_per_pr`, `require_test_changes`, plus provenance knobs.

### Algorithm

1. `gh pr list --state merged --json ...` — filter mergeAts and skip drafts client-side.
2. Fetch `base.sha` per PR via `github.fetch_pr` (patched in #73 — `gh pr list --json baseRefOid` doesn't populate).
3. Per PR: split into `(source_patch, test_patch)`, apply structural filters, drop drafts and CI-only changes.
4. Emit a Harbor task with the thin env: `python:3.12-slim` + repo clone at `base_commit` + base64-baked `/verifier/oracle.patch`, `/verifier/instruction.md`, `/verifier/verifier.py`.
5. **No sandbox bootstrap.** The Dockerfile is self-contained; consumers rebuild it in ~30 s.

### Output

- Task shape: `environment/Dockerfile` (self-contained), `tests/test.sh` (diff-then-invoke-verifier), `solution/{patch.diff, solve.sh}`, `instruction.md`, `task.toml`.
- `[metadata.repo2env]` provenance: `pipeline`, `repo`, `ref` (= base_commit), `reference` (PR URL), `reward_kinds=["diff_similarity"]`, `reward_calibration` (LOC + difficulty bucket).

## Verification

- **Reward kind** — `diff_similarity`.
- **Reward formula** — weighted sum of 6 components:

  | Component | Weight | Captures |
  |---|--:|---|
  | `format_valid` | 0.00 | Guard: parses as a unified diff (weight 0; kept as a bit) |
  | `size_sanity` | 0.08 | `min(oracle_loc, predicted_loc) / max(...)` |
  | `file_targeting` | 0.12 | F1 over changed-file sets |
  | `region_overlap` | 0.20 | Predicted hunks overlap oracle hunks (5-line slack) |
  | `similarity` | 0.10 | `SequenceMatcher` over `+`/`-` lines only |
  | `llm_judge` | 0.50 | Haiku 4.5 semantic correctness rating |

  Plus a **catastrophic-size cap** — clamps reward to ≤ 0.40 when `size_sanity < 0.10`.

- **Oracle invariant** — the merged diff scores exactly 1.0.
- **Non-tamper** — verifier reads the agent's diff from `git diff --cached <base_commit>` and scores it against the oracle; the agent has no test file to tamper with.

## Anti-contamination

- **Git-history scrub** — after checkout at `base_commit`, remove `origin`, prune future refs, `gc`. Prevents `git diff origin/main`.
- **Egress guard** — the shared `_env_guard.py` `docker-compose.yaml` overlay blackholes PyPI + GitHub, so `pip download <pkg>==<fix>` and web fetches of the fix commit fail.
- **Instruction leak-strip** — extended in v0.8.5 to catch trailing `(#NNNN)` squash-trailers and cross-repo `repo#N` refs.

## LLM use

- **`at verify` (per scoring)** — one Anthropic Haiku call per agent invocation, weight 0.50. Graceful degradation on missing API key: `judge_status=no_api_key`, other 5 components renormalize.
- **No bootstrap LLM** — the thin env doesn't need it.
- **Cost order-of-magnitude** — ~$0.001-$0.005 per scoring call. A 100-agent-run × 100-task eval ≈ $10-50.

## Yield & repo suitability

- **80–95% yield** — text-only, no execution gate. Almost every merged PR qualifies.
- **Works on any repo with merged PRs.** No sandbox constraint means monorepos, ML repos with non-portable test suites, and GPU-only projects all mine cleanly.

## Dependencies

- No reuse; `pr_diff` is the *base* pipeline. Later pipelines borrow its Dockerfile-baking + verifier pattern.
- Stdlib + `difflib`. LLM judge via LiteLLM.

## Alternatives considered

- **Full sandbox-verified diff similarity** — deferred to `pr_runtime`. `pr_diff` deliberately stays text-only so it's the cheapest, broadest baseline.
- **Deterministic weights only, no LLM judge** — tried; scored `format_valid` + `similarity` too high (pilot data). LLM judge earns its 0.50 weight.

## Rollout plan

Historic. Shipped in v0.1.0. Retuned in v0.8.3 via an LLM-driven reward-engineering pass on a 23-task pilot: Sonnet analyzed per-task component data and recommended the current weights (data-grounded; the original guesses were wrong).

## Open questions

Historic — none active.

## References

- SWE-RL: [arXiv:2502.18449](https://arxiv.org/abs/2502.18449), [facebookresearch/swe-rl](https://github.com/facebookresearch/swe-rl).
- Reward-tuning pilot: [`docs/release_notes/v0.8.3/findings-pr_diff.md`](../release_notes/v0.8.3/findings-pr_diff.md).

## Implementation

| | |
|---|---|
| **Initial PR** | Multiple commits pre-#4 (`be7a0f5` renamed; earlier commits landed the pipeline) |
| **Shipping release** | v0.1.0 |
| **Source file** | [`src/repo2rlenv/pipelines/pr_diff.py`](../../src/repo2rlenv/pipelines/pr_diff.py) |
| **Verifier** | [`src/repo2rlenv/pipelines/_pr_diff_verifier.py`](../../src/repo2rlenv/pipelines/_pr_diff_verifier.py) |
| **Options model** | [`src/repo2rlenv/spec/options.py`](../../src/repo2rlenv/spec/options.py) — `PrDiffOptions` |
| **Doc page** | [`docs/pipelines/pr_diff.md`](../pipelines/pr_diff.md) |
| **Findings / release notes** | [`docs/release_notes/v0.8.3/findings-pr_diff.md`](../release_notes/v0.8.3/findings-pr_diff.md) |
| **Reference dataset** | [`AdithyaSK/repo2rlenv-pr-diff`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-diff) (181 envs) |
| **Follow-up PRs** | [#40](https://github.com/huggingface/Repo2RLEnv/pull/40) Harbor-runnable env + 6-component reward · [#63](https://github.com/huggingface/Repo2RLEnv/pull/63) GitLab source support · [#73](https://github.com/huggingface/Repo2RLEnv/pull/73) fetch base_sha via REST · [#76](https://github.com/huggingface/Repo2RLEnv/pull/76) mirror #75 (Harbor spec: reward-details.json sidecar) |
