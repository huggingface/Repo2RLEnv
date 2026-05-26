# `pr_diff` — Harbor-runnable env + 6-component reward

This release lifts `pr_diff` from v0.1's text-only output to a **fully Harbor-runnable RL environment** with a multi-component diff-similarity verifier (5 deterministic components + LLM-as-judge). The PR ships **100 verified environments published to HF Hub** as the reference dataset.

## Published dataset

**<https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-diff>**

- 100 environments
- 26 source repos across Tier A (SWE-bench Python), Tier B (HF ecosystem), Tier C (Go / Rust / Node / TS)
- Visualiser badge embedded for in-browser inspection
- License: Apache-2.0

Pull + use:

```bash
repo2rlenv pull AdithyaSK/repo2rlenv-pr-diff /tmp/pr-diff
repo2rlenv validate /tmp/pr-diff
harbor run -p /tmp/pr-diff -a oracle --env docker  # → reward = 1.000
```

## What changed

### 1. Harbor-runnable environment (was text-only in v0.1)

Each emitted task now ships `environment/Dockerfile` + `tests/test.sh`, so `harbor run` works directly. The Dockerfile is a thin, **agent-agnostic** `python:3.12-slim` + git image with the repo cloned at `base_commit` and the oracle diff base64-baked in — Harbor's agent adapter installs whatever runtime the agent needs (claude-code / openhands / codex / aider). No bootstrap LLM agent — image builds in ~30 s.

The verifier is a pure-stdlib Python module ([`_pr_diff_verifier.py`](../../../src/repo2rlenv/pipelines/_pr_diff_verifier.py)) base64-embedded in `tests/test.sh`. It captures the agent's edits via `git add -A; git diff --cached <base>` and scores them against the oracle.

### 2. Six-component multi-component reward

Replaces the single-scalar `difflib.ratio()` with the SWE-RL-paper's recipe:

| Component | Weight | What it captures |
|---|--:|---|
| `format_valid` | 0.00 | Predicted parses as a unified diff. Always 1 for `claude-code` → no discriminative signal → weight 0 (kept as a guard). |
| `size_sanity` | 0.08 | `min(oracle_loc, predicted_loc) / max(...)`. Catches over/under-generation. |
| `file_targeting` | 0.12 | F1 over the changed-file sets (not Jaccard — F1 properly credits TP). |
| `region_overlap` | 0.20 | Predicted hunks overlap oracle hunks (5-line slack). |
| `similarity` | 0.10 | `SequenceMatcher` ratio over `+`/`-` lines only (no free credit for context). |
| `llm_judge` | 0.50 | Haiku rates semantic correctness. Graceful degradation on missing API key. |

Plus a **catastrophic-size hard cap**: clamps final reward to ≤ 0.40 when `size_sanity < 0.10`. Stops a charitable judge from inflating scores on patches that are wildly the wrong size.

Final weights were retuned via an LLM-driven reward-engineering pass on a 23-task pilot — Sonnet 4.6 analyzed the per-task component data and recommended these weights (data-grounded; the original guesses scored `format_valid` and `similarity` too high).

### 3. Per-task calibration baseline + difficulty bucket

Every task carries `task.toml.metadata.repo2env.reward_calibration`:

```toml
[metadata.repo2env.reward_calibration]
baseline_reward = 0.0
loc_changed = 95
difficulty = "large"
```

Consumers can normalize: `calibrated = (raw - baseline) / (1 - baseline)`. `calibrated < 0` means the agent did worse than no-op.

Difficulty buckets: trivial (≤ 5 LOC), small (6 – 20), medium (21 – 80), large (> 80).

### 4. Broadened instruction info-leak strip (8 pattern families)

PR descriptions frequently include pointers to the answer. The pipeline now strips all of:

1. Multi-issue `Closes #1, #2, #3`
2. `See` / `refs` / `follow-up to` linkbacks
3. Markdown issue links `[#1234](url)`
4. Closes with markdown-link refs `Closes [#1234](url)`
5. Descriptive markdown links to GH URLs `[my analysis](https://github.com/x/y/pull/1234)`
6. Bare GitHub URLs (including `redirect.github.com` from Dependabot)
7. Commit trailers (`Co-authored-by`, `Signed-off-by`, etc.)
8. Title squash suffix `(fixes #1234)` / `(#1234)`

Composite patterns are stripped before piece-wise ones so we don't leave orphaned `Closes ` keywords or empty `[text]()` brackets behind.

### 5. Quality filters at generation time

New gen-time filter drops candidates that obviously make weak RL tasks:

- 100% test-file changes (no source code touched)
- 100% docs-only changes
- Reverts (`Revert "..."` title)
- Empty body after info-leak strip AND short title (< 5 words)
- Oracle diff with fewer than `min_loc_changed=3` `+`/`-` lines

These filters caused 1 of the 25 launch repos (`date-fns/date-fns`) to emit 0 qualifying tasks from its recent PR history; the dataset is topped up from a backup repo (`tiangolo/typer`) to reach 100.

## How the 100 envs were generated

The published dataset is reproducible from the public `repo2rlenv` CLI alone — no internal tooling required. The recipe is per-repo `generate` × 25 repos, sequenced with concurrency 5 and `--max-retries 2` on the harbor side:

```bash
# Per repo (× 25), fetch ~5× the needed envs so quality filters can drop the
# weak ones while still leaving 4 per repo:
repo2rlenv generate --repo <owner>/<repo> --pipeline pr_diff \
    --pipeline-opt limit=20 \
    --out /tmp/pr-diff-100/staged/<repo>

# Top up to exactly 100 from a backup repo (date-fns yielded 0 qualifying PRs):
repo2rlenv generate --repo tiangolo/typer --pipeline pr_diff \
    --pipeline-opt limit=20 --out /tmp/pr-diff-100/staged/typer

# Merge all per-repo dirs into one dataset/ and push:
repo2rlenv push /tmp/pr-diff-100/dataset AdithyaSK/repo2rlenv-pr-diff
```

The 25 source repos span SWE-bench Python anchors, the HF ecosystem, and a multi-language slice (Go / Rust / Node / TS). Quality filters drop test-only / docs-only / reverts / oracle-too-small candidates before they reach the dataset.

## Validation evidence (smoke pilots)

Across three smoke runs (limit=1 → limit=5 → full 100), every successfully-completed oracle trial scored `final_reward = 1.000`. Sonnet 4.6 spread on a 23-trial pilot: median 0.71, range 0.16 – 0.98, judge component fired with `status=ok` on ~95% of trials. The env produces a meaningful eval distribution; no degenerate bunching.

Two real verifier bugs were found and fixed by the pilots:

1. `git diff <base>` skips untracked files → PRs that add files silently scored low. Fix: `git add -A; git diff --cached <base>`.
2. Harbor's claude-code agent setup (`curl claude.ai/install.sh`) saturates local bandwidth at concurrency ≥ 12, triggering `AgentSetupTimeoutError`. Fix: keep concurrency ≤ 5 and pass `--max-retries 2` to harbor. **Not** baking the agent into the task Dockerfile — that would couple every env to one vendor's CLI and violate the agent-agnostic contract of a Harbor task spec.

## Limitations

- The reward is a *single scalar* (the weighted sum) by default. Consumers wanting the per-component breakdown should read `/logs/verifier/reward.json` written by every verifier run.
- `similarity` (now weight 0.10) still penalizes legitimate alternative implementations. For pure SWE-RL-style training corpora that's fine; for capability eval, treat similarity as a coarse signal.
- The LLM-as-judge in the reward is a network call. For high-throughput RL training rollouts (millions of evals) it becomes a real cost item. Disable via `R2E_W_JUDGE=0` to fall back to the deterministic-only score.
