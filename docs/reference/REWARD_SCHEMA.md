# Reward Schema Reference

Every task emitted by Repo2RLEnv writes its reward to `/logs/verifier/` inside
the container after the agent's patch is applied and the verifier runs. Harbor
reads `reward.txt` as the primary training signal; `reward.json` carries the
full breakdown for analysis, filtering, and debugging.

## Files written per task

| File | Always present? | Content |
|---|:-:|---|
| `/logs/verifier/reward.txt` | ✅ | Single float on one line, e.g. `0.842731` |
| `/logs/verifier/reward.json` | Pipeline-dependent (see below) | Full breakdown as JSON |

---

## `pr_diff`

**Reward kind:** `diff_similarity`

**`reward.txt`:** weighted combination of 6 components, clamped to `[0, 1]`.

**`reward.json`:**

```json
{
  "reward": 0.74,
  "components": {
    "format_valid":   1.0,
    "size_sanity":    0.85,
    "file_targeting": 0.67,
    "region_overlap": 0.60,
    "similarity":     0.55,
    "llm_judge":      0.80
  },
  "weights": {
    "format_valid":   0.00,
    "size_sanity":    0.08,
    "file_targeting": 0.12,
    "region_overlap": 0.20,
    "similarity":     0.10,
    "llm_judge":      0.50
  },
  "judge_model":  "anthropic/claude-haiku-4-5-20251001",
  "judge_status": "ok",
  "capped":       false
}
```

**Field reference:**

| Field | Type | Description |
|---|---|---|
| `reward` | float [0, 1] | Final score. Hard-capped to ≤ 0.40 when `size_sanity < 0.10` (catastrophic-size guard). |
| `components.format_valid` | 0 or 1 | Predicted output parses as a valid unified diff. Weight 0.00 — kept as a guard, not a scoring factor. |
| `components.size_sanity` | [0, 1] | `min(oracle_loc, pred_loc) / max(oracle_loc, pred_loc)` — detects severe over- or under-generation. |
| `components.file_targeting` | [0, 1] | F1 over the sets of changed files (not Jaccard — F1 gives partial credit for TP). |
| `components.region_overlap` | [0, 1] | Predicted hunks overlap oracle hunks within a 5-line slack. |
| `components.similarity` | [0, 1] | `difflib.SequenceMatcher` ratio over `+`/`-` lines only (no credit for context lines). |
| `components.llm_judge` | [0, 1] or `null` | LLM semantic judge ("does this address the issue?"). `null` when disabled or API key absent — weight redistributed to remaining components. |
| `weights` | object | Effective per-component weights (overridable via `R2E_W_*` env vars). |
| `judge_model` | string or `null` | Model used for `llm_judge`, or `null` if judge was skipped. |
| `judge_status` | string | `"ok"` \| `"no_api_key"` \| `"error"` \| `"timeout"` |
| `capped` | bool | `true` if the hard size-sanity cap was applied (`reward` forced to ≤ 0.40). |

**Weight override env vars** (set inside the verifier container via `--ve`):
`R2E_W_FORMAT`, `R2E_W_SIZE`, `R2E_W_FILE`, `R2E_W_REGION`, `R2E_W_SIM`, `R2E_W_JUDGE`

---

## `pr_runtime` · `commit_runtime` · `cve_patches`

**Reward kind:** `test_execution` (primary) + `diff_similarity` (fallback)

**`reward.txt`:** `f2p_rate × p2p_rate`, rounded to 6 decimal places.

**`reward.json` (normal path — test output successfully parsed):**

```json
{
  "reward":                  0.833333,
  "resolved":                false,
  "command_resolved":        false,
  "f2p_total":               3,
  "f2p_passed":              2,
  "f2p_rate":                0.666667,
  "p2p_total":               5,
  "p2p_passed":              5,
  "p2p_rate":                1.0,
  "regressions":             [],
  "untracked_failed_count":  1,
  "untracked_failed":        ["tests/test_other.py::test_legacy"],
  "parse_status":            "ok",
  "runner":                  "pytest",
  "tests_parsed":            12,
  "exit_code":               1
}
```

**`reward.json` (fallback path — test output unrecognised, no per-test status):**

```json
{
  "reward":            1.0,
  "resolved":          false,
  "command_resolved":  true,
  "parse_status":      "fallback_exitcode",
  "eval_trustworthy":  false,
  "runner":            "",
  "f2p_total":         3,
  "p2p_total":         5,
  "exit_code":         0
}
```

**Field reference:**

| Field | Type | Description |
|---|---|---|
| `reward` | float [0, 1] | Dense training signal: `f2p_rate × p2p_rate`. Gold patch → 1.0. |
| `resolved` | bool | **SWE-bench eval signal.** All F2P tests pass AND all P2P tests pass. Gold patch → `true`. Use this for benchmark-style scoring. |
| `command_resolved` | bool | Stricter: `resolved` AND no untracked failures AND `exit_code == 0`. Filters tasks with pre-existing flaky tests outside F2P/P2P. |
| `f2p_total` | int | Number of declared FAIL_TO_PASS tests. |
| `f2p_passed` | int | F2P tests that passed after the agent's patch. |
| `f2p_rate` | float [0, 1] | `f2p_passed / f2p_total`. 0.0 when `f2p_total == 0`. |
| `p2p_total` | int | Number of declared PASS_TO_PASS tests. |
| `p2p_passed` | int | P2P tests still passing after the agent's patch. |
| `p2p_rate` | float [0, 1] | `p2p_passed / p2p_total`. **1.0 when `p2p_total == 0`** (no regression guard). |
| `regressions` | list[str] | P2P tests that broke under the agent's patch. |
| `untracked_failed_count` | int | Tests that failed but were not in F2P or P2P — often pre-existing flakiness. |
| `untracked_failed` | list[str] | Names of untracked failures (capped at 20). |
| `parse_status` | string | `"ok"` — per-test status parsed successfully. `"fallback_exitcode"` — runner output unrecognised, `reward` is binary exit-code based. |
| `eval_trustworthy` | bool | Only present in `fallback_exitcode` path. `false` when an F2P oracle exists but couldn't be verified. |
| `runner` | string | Detected test runner: `"pytest"` \| `"go"` \| `"cargo"` \| `"jest"` \| `""` |
| `tests_parsed` | int | Total tests detected in log output (only in `parse_status == "ok"`). |
| `exit_code` | int | Raw exit code of the test command. |

**Choosing between `reward`, `resolved`, and `command_resolved`:**

- **Training:** use `reward` (dense, graded signal from `reward.txt`).
- **Benchmark / leaderboard:** use `resolved` (strict SWE-bench tracked resolution, unaffected by untracked flakiness).
- **Strict eval (clean command required):** use `command_resolved`.

---

## `code_instruct` · `equivalence_tests`

**Reward kind:** `test_execution`

**`reward.txt`:** `1.0` (tests pass) or `0.0` (tests fail). Binary.

**`reward.json`:** not written by these pipelines.

The verifier runs the repo's test suite (or the generated pytest equivalence
tests) and maps the exit code directly to reward. No per-component breakdown.

**Reading the reward for training:**

```python
reward = float(open("/logs/verifier/reward.txt").read().strip())  # 1.0 or 0.0
```

---

## Per-pipeline summary

| Pipeline | `reward.txt` | `reward.json` | Training signal | Eval signal |
|---|---|:-:|---|---|
| `pr_diff` | weighted [0,1] | ✅ | `reward` | `components` breakdown |
| `pr_runtime` | f2p × p2p | ✅ | `reward` | `resolved` / `command_resolved` |
| `commit_runtime` | f2p × p2p | ✅ | `reward` | `resolved` / `command_resolved` |
| `cve_patches` | f2p × p2p | ✅ | `reward` | `resolved` / `command_resolved` |
| `code_instruct` | 1.0 / 0.0 | ❌ | `reward` | `reward` |
| `equivalence_tests` | 1.0 / 0.0 | ❌ | `reward` | `reward` |
