# `pr_runtime` — graded F2P/P2P reward + scale audit (Arc 2)

This release upgrades `pr_runtime` from a binary pass/fail test-execution reward to a **graded** reward that gives RL training a dense gradient while still serving strict SWE-bench evaluation, and ships **100 oracle-verified environments** as the reference dataset.

## Published dataset

**<https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-runtime>**

- 100 environments, all oracle-verified (reward 1.0 with the gold patch). (5 merge-forward "Merge stable into main" tasks were removed in an audit pass — see Limitations; clean count > round number.)
- **63 Python · 37 Go**, across 13 repos
- **Reproducible without registry creds**: each task's `environment/Dockerfile` is a clean recipe (`FROM <base>` → git clone the repo → checkout the bootstrap ref → run the verified `rebuild_cmds`), so consumers rebuild the env from scratch. Verified end-to-end: `docker build` succeeds and `harbor run -a oracle` scores 1.0 on the rebuilt image. (The earlier bootstrap "dockerfile_reconstruction" baked the agent's transcript — command *output* as RUN lines — and did not build; fixed to use `rebuild_cmds`.)
- Difficulty spread: trivial 18 · small 31 · medium 38 · large 12
- FAIL_TO_PASS: 1–462 tests/task (avg 8.4) · PASS_TO_PASS: 0–1048 (avg 401)
- **Per-task validation manifest** (`manifest.json` in the dataset): one row per task — repo, PR URL, base commit, language, F2P/P2P counts, difficulty, oracle reward/resolved — so the benchmark composition is machine-checkable (all 99 rows: `oracle_reward=1.0, resolved=true`).
- **Full leak re-audit**: all 99 instructions scanned — no dangerous solution leaks (residual `#refs` / failing-test tracebacks are realistic problem-statement content, as in SWE-bench).

## What changed

### 1. Graded F2P/P2P reward (was binary)

`tests/test.sh` used to write `1.0` if the suite exited 0 else `0.0`. An agent that fixed 4 of 5 failing tests scored the same `0.0` as one that fixed nothing — a poor RL gradient. A new in-container verifier ([`_pr_runtime_verifier.py`](../../../src/repo2rlenv/pipelines/_pr_runtime_verifier.py), pure stdlib, base64-baked) now scores:

```
reward = f2p_rate × p2p_rate        # f2p_rate = F2P now passing / total
                                    # p2p_rate = P2P still passing / total
resolved = (all F2P pass) AND (all P2P pass)   # strict SWE-bench bool
```

It emits **both** signals: `reward.txt` carries the graded scalar (training), `reward.json` carries the strict `resolved` bool (eval) plus the full breakdown. **Oracle invariant**: the gold patch flips all F2P and keeps all P2P → reward 1.0, so the oracle gate is unchanged. Ports the 4 log parsers (pytest/go/cargo/jest); falls back to the exit-code reward on unparseable output.

### 2. The scale audit — four real fixes

Auditing the smoke output + agent traces before scaling surfaced four issues, all fixed:

1. **F2P detection was silently broken on real repos** (critical). The validation markers went to stderr (via `set -x`) while test output went to stdout, and the log was truncated to 20 KB vs a real ~140 KB suite → **zero tests parsed → every candidate dropped on `no_fail_to_pass`**. Fix: echo markers to stdout; parse the full output. On `pallets/click` this took emission **0 → 12** and dropped `no_fail_to_pass` 12 → 1.
2. **Instructions leaked the solution** (eval integrity). Built from the PR body, they handed the agent commit SHAs to cherry-pick, the fix approach, and the names of the grading tests — Sonnet exploited a leaked commit `c3535905` 28× in one trace. Fix: source the problem statement from the linked **issue** (bug report), extend the leak-strip (SHAs / fix-PR refs / markdown issue links), drop "Tests added" sections, and filter non-bug PRs (backport/cherry-pick/release/revert).
3. **`git clean -fdx` wiped polyglot deps** — excluded only Python venv dirs, so resetting to a PR base would delete `node_modules` / `target` / `vendor`, yielding 0 for Node/Rust. Fix: exclude those dep/build dirs.
4. **Missing ca-certificates** — minimal images (node:alpine) lacked CA certs, so in-container HTTPS `git fetch` of base commits failed verification. Fix: install ca-certificates in the validation install + env Dockerfile.

### 3. Difficulty + coverage metadata

Each task stamps `[metadata.repo2env.reward_calibration]` with `f2p_count`, `p2p_count`, `source_files`, `loc_changed`, and a `difficulty` bucket — so consumers can slice train/eval by hardness and judge regression-guard strength (`p2p_count == 0` = weak guard, per UTBoost).

## Validation evidence

- **Oracle gate**: 100/100 score reward 1.0 (`resolved=True`). A handful of candidates scoring 0.90–0.99 (flaky P2P) were dropped — only clean 1.0 tasks ship.
- **Discriminating difficulty + dense reward**: on a ~20-task stratified Sonnet 4.6 sample, the solve rate lands ~55–60% (e.g. 7 resolved / 2 partial / 3 failed of the first 12), squarely in the "useful eval" band (30–80%) — vs the artificial 100% the leaky instructions produced before the audit fixes. Crucially, **partial-credit scores appear in the wild** (e.g. `0.57`, `0.12`) — the dense gradient the graded reward was built for, not binary 0/1.
- The graded reward fires correctly: e.g. `reward 1.0, resolved true, f2p 3/3, p2p 595/595`.

## Limitations

- **Rust yields little**: Rust tests are usually inline (`#[cfg(test)] mod tests` inside `src/*.rs`), so the path-based test/source split files them as source → "no test patch." Detecting inline `#[test]` hunks is a v0.9 item. The dataset is Python + Go.
- **No flaky-test tolerance**: validation is single-run; a flaky P2P can cause a false regression. SWE-bench Verified handled this with manual curation. We drop sub-1.0 oracle tasks, which removes the worst offenders.
- **Merge-forward tasks removed (audit)**: an expert audit found 5 "Merge stable into main" branch-sync PRs had slipped past the non-bug filter (broad multi-area diffs, not focused bug fixes). The filter now catches `merge … into`, `merge stable/main`, `sync stable`, etc., and those 5 tasks were dropped — leaving 99 clean tasks; one more oracle-verified urfave/cli task was added to round to 100.
- **Hidden-test integrity**: `tests/test.sh` now fails CLOSED if the hidden test_patch doesn't apply (reward 0 + `parse_status=test_patch_apply_failed`), so an agent can't get credit by breaking patch application. And the unparseable-log fallback no longer reports `resolved: true` when an F2P oracle is declared (no per-test evidence ⇒ not resolved; reward stays a coarse training-only signal flagged `eval_trustworthy: false`).
- **Repo concentration**: the productive repos (click, urfave/cli, werkzeug) dominate the set, since the big/quirky repos (pytest, sphinx, pydantic) yield few separable bug-fix-with-test PRs. Comparable to SWE-bench's repo skew.

## How it was generated

```bash
# Per repo: bootstrap (LLM-built Docker env, cached) + mine/validate PRs
repo2rlenv generate --repo <owner>/<repo> --pipeline pr_runtime \
  --pipeline-opt limit=60 --llm anthropic/claude-sonnet-4-6 \
  --out ./stage/<repo>
# Oracle-gate (keep reward 1.0), balance, push
harbor run -p ./pool -a oracle --env docker          # gate
repo2rlenv push ./dataset AdithyaSK/repo2rlenv-pr-runtime
```
