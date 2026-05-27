# `pr_runtime` — graded F2P/P2P reward + scale audit (Arc 2)

This release upgrades `pr_runtime` from a binary pass/fail test-execution reward to a **graded** reward that gives RL training a dense gradient while still serving strict SWE-bench evaluation, and ships **100 oracle-verified environments** as the reference dataset.

## Published dataset

**<https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-runtime>**

- 100 environments, all oracle-verified (reward 1.0 with the gold patch). Resolution split (rev 2): **100 `resolved`** (tracked) · **88 `command_resolved`** (strict) · **87 `eval_grade`** (strict + regression guard — the benchmark-grade subset). (5 merge-forward "Merge stable into main" tasks were removed in an earlier audit pass — see Limitations; clean count > round number.)
- **63 Python · 37 Go**, across 13 repos
- **Reproducible without registry creds**: each task's `environment/Dockerfile` is a clean recipe (`FROM <base>` → git clone the repo → checkout the bootstrap ref → run the verified `rebuild_cmds`), so consumers rebuild the env from scratch. Verified end-to-end: `docker build` succeeds and `harbor run -a oracle` scores 1.0 on the rebuilt image. (The earlier bootstrap "dockerfile_reconstruction" baked the agent's transcript — command *output* as RUN lines — and did not build; fixed to use `rebuild_cmds`.)
- Difficulty spread: trivial 18 · small 31 · medium 38 · large 12
- FAIL_TO_PASS: 1–462 tests/task (avg 8.4) · PASS_TO_PASS: 0–1048 (avg 401)
- **Per-task validation manifest** (`manifest.json` in the dataset): one row per task — repo, PR URL, base commit, F2P/P2P counts, difficulty, plus a `validation` block (build status, oracle reward, `resolved`/`command_resolved`/`eval_grade`, exit code, parser status, tests parsed, runtime) and **sha256 checksums** of all six task artifacts. The top level records the **dataset commit** the gate ran against and the `repo_distribution`. Machine-checkable composition (all 100 rows `oracle_reward=1.0, resolved=true`; 88 `command_resolved`, 87 `eval_grade`).
- **Full leak re-audit**: all 100 instructions scanned — no dangerous solution leaks (residual `#refs` / failing-test tracebacks are realistic problem-statement content, as in SWE-bench).
- **Plain task artifacts**: each task ships `tests/verifier.py` + `tests/f2p.json` + `tests/p2p.json` as inspectable files (Harbor mounts `tests/` at `/tests`); `test.sh` is a thin orchestrator — no base64 blobs. Verified end-to-end on Python + Go (`docker build` + oracle 1.0).

## What changed

### 1. Graded F2P/P2P reward (was binary)

`tests/test.sh` used to write `1.0` if the suite exited 0 else `0.0`. An agent that fixed 4 of 5 failing tests scored the same `0.0` as one that fixed nothing — a poor RL gradient. A new in-container verifier ([`_pr_runtime_verifier.py`](../../../src/repo2rlenv/pipelines/_pr_runtime_verifier.py), pure stdlib, base64-baked) now scores:

```
reward           = f2p_rate × p2p_rate     # f2p_rate = F2P now passing / total
                                           # p2p_rate = P2P still passing / total
resolved         = (all F2P pass) AND (all P2P pass)        # tracked (SWE-bench)
command_resolved = resolved AND no untracked failures AND exit_code == 0  # strict
```

It emits **both** signals: `reward.txt` carries the graded scalar (training), `reward.json` carries the resolution bools (eval) plus the full breakdown (`exit_code`, `untracked_failed_count`, regressions). **Oracle invariant**: the gold patch flips all F2P and keeps all P2P → reward 1.0 and `resolved=true`, so the oracle gate is unchanged. The two-signal split is explained in rev 2 below. Ports the 4 log parsers (pytest/go/cargo/jest); falls back to the exit-code reward on unparseable output (never claiming `resolved` when an F2P oracle is declared but no per-test evidence exists).

### 2. The scale audit — four real fixes

Auditing the smoke output + agent traces before scaling surfaced four issues, all fixed:

1. **F2P detection was silently broken on real repos** (critical). The validation markers went to stderr (via `set -x`) while test output went to stdout, and the log was truncated to 20 KB vs a real ~140 KB suite → **zero tests parsed → every candidate dropped on `no_fail_to_pass`**. Fix: echo markers to stdout; parse the full output. On `pallets/click` this took emission **0 → 12** and dropped `no_fail_to_pass` 12 → 1.
2. **Instructions leaked the solution** (eval integrity). Built from the PR body, they handed the agent commit SHAs to cherry-pick, the fix approach, and the names of the grading tests — Sonnet exploited a leaked commit `c3535905` 28× in one trace. Fix: source the problem statement from the linked **issue** (bug report), extend the leak-strip (SHAs / fix-PR refs / markdown issue links), drop "Tests added" sections, and filter non-bug PRs (backport/cherry-pick/release/revert).
3. **`git clean -fdx` wiped polyglot deps** — excluded only Python venv dirs, so resetting to a PR base would delete `node_modules` / `target` / `vendor`, yielding 0 for Node/Rust. Fix: exclude those dep/build dirs.
4. **Missing ca-certificates** — minimal images (node:alpine) lacked CA certs, so in-container HTTPS `git fetch` of base commits failed verification. Fix: install ca-certificates in the validation install + env Dockerfile.

### 3. Difficulty + coverage metadata

Each task stamps `[metadata.repo2env.reward_calibration]` with `f2p_count`, `p2p_count`, `source_files`, `loc_changed`, and a `difficulty` bucket — so consumers can slice train/eval by hardness and judge regression-guard strength (`p2p_count == 0` = weak guard, per UTBoost).

### 4. Second audit revision — tracked vs. command resolution, recipe robustness, enriched manifest

A follow-up expert audit ran a full 100-task oracle gate from the published dataset and found two P0s plus three P1/P2 items. All resolved:

1. **`resolved: true` could mask a failing test command** (P0). Running a *whole test file* sometimes pulls in pre-existing/flaky failures outside the F2P/P2P sets (e.g. `encode/httpx`'s `cp1252` codec tests). The verifier scored those tasks `resolved: true` even though the command exited non-zero. Fix (the audit's accepted "tracked vs command" alternative): keep **`resolved`** = SWE-bench tracked resolution (all F2P+P2P pass — the gold patch always satisfies it, so the oracle invariant holds) and add **`command_resolved`** = `resolved` AND zero untracked failures AND `exit_code == 0`. `exit_code` and `untracked_failed_count` are now in *every* `reward.json`. The 100-task gate: **100 tracked-resolved, 88 command-resolved** — the 12-task gap is exactly the untracked-failure tasks, now flagged out of strict eval instead of silently inflating it.
2. **An environment built but couldn't run its tests** (P0). `pallets/werkzeug` declares its test deps via PEP 735 `[dependency-groups]`, not `[project.optional-dependencies]`, so `pip install -e '.[tests]'` was a silent no-op (pip only warns "does not provide the extra 'tests'") — neither `pytest` nor `pytest-timeout` got installed, and collection errored under `filterwarnings = error`. Fix: the inline recipe now runs `pip install -U pip && pip install --group tests` (the PEP 735 group) with a bare-`pytest` fallback, guaranteeing the runner + its plugins for every Python task. Verified: werkzeug tasks flip `reward 0.0 → 1.0`.
3. **Enriched, oracle-stamped manifest** (P1). `manifest.json` now records per-task build status, oracle reward, both resolution bools, exit code, parser status, tests parsed, runtime, and **sha256 checksums** of all six task artifacts, plus the **dataset commit** the validation ran against and the top-level `repo_distribution`. `push` preserves an enriched manifest instead of regenerating a minimal one (it can't run an oracle), and the dataset card renders the validation summary + skew table.
4. **Benchmark-grade subset + skew disclosure** (P1/P2). Added an **`eval_grade`** flag = `command_resolved` AND `p2p_count > 0` (a real regression guard) → **87/100**; `psf/requests-7315` (zero P2P) is correctly excluded from strict eval but kept for training. The README states the repo skew (click 28 · urfave/cli 25 · werkzeug 16 · others 31) as a benchmark limitation and points evaluators at `eval_grade == true`.
5. **One flaky task repaired, not dropped.** `urfave/cli-2290`'s lone failing P2P (`TestCompletionShell`, order/env-dependent — passed on re-run) was pruned from its P2P set rather than dropping the whole task, keeping the count at 100 and the balance intact.

## Validation evidence

- **Oracle gate**: a full 100-task gate from the published commit scores reward 1.0 and `resolved=True` on all 100 (rev 2); 88 are `command_resolved` (clean test command) and 87 are `eval_grade` (also a non-empty P2P guard). Per-task results are stamped into `manifest.json`.
- **Discriminating difficulty + dense reward**: on a ~20-task stratified Sonnet 4.6 sample, the solve rate lands ~55–60% (e.g. 7 resolved / 2 partial / 3 failed of the first 12), squarely in the "useful eval" band (30–80%) — vs the artificial 100% the leaky instructions produced before the audit fixes. Crucially, **partial-credit scores appear in the wild** (e.g. `0.57`, `0.12`) — the dense gradient the graded reward was built for, not binary 0/1.
- The graded reward fires correctly: e.g. `reward 1.0, resolved true, f2p 3/3, p2p 595/595`.

## Limitations

- **Rust yields little**: Rust tests are usually inline (`#[cfg(test)] mod tests` inside `src/*.rs`), so the path-based test/source split files them as source → "no test patch." Detecting inline `#[test]` hunks is a v0.9 item. The dataset is Python + Go.
- **Limited flaky-test tolerance**: validation is single-run; a flaky P2P can cause a false regression. SWE-bench Verified handled this with manual curation. Rev 2 added two guards: untracked failures no longer poison tracked `resolved` (they only drop `command_resolved`), and a confirmed-flaky test can be pruned from a task's P2P set (done for `urfave/cli-2290`'s `TestCompletionShell`) rather than dropping the task.
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
