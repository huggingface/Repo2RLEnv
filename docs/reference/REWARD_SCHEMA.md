---
title: "Reward Schema Reference"
navTitle: "Reward schema"
---

Every task emitted by Repo2RLEnv writes its reward to `/logs/verifier/` inside
the container after the agent's patch is applied and the verifier runs. Harbor
reads `reward.txt` as the primary training signal. Native pipelines with a
graded verifier also write `reward-details.json`, the full breakdown for
analysis, filtering, and debugging.

## Files written per task

| File | Always present? | Content |
|---|:-:|---|
| `/logs/verifier/reward.txt` | ✅ unless the run is ungraded (native runtime pipelines, below) | Single float on one line, e.g. `0.842731` |
| `/logs/verifier/reward-details.json` | Pipeline-dependent (see below) | Full breakdown as JSON (`pr_diff`, `pr_runtime`, `commit_runtime`, `cve_patches`, `code_instruct`, `equivalence_tests`) |
| `/logs/verifier/submission.patch`, `submission.json` | `pr_runtime` family | The source diff that was graded and how the agent's tree became that diff |
| `/logs/verifier/result.json` | Research recipes and Tasksmith | The grader's outcome, e.g. `{"passed": true, "returncode": 0, "statuses": {…}}`; SCALER writes `{"score": …, "accuracy": …}`. CLI-Gym writes `reason.txt` instead when protected source was changed. |

**Ungraded runs (native runtime pipelines).** `pr_runtime`, `commit_runtime`,
`cve_patches`, `code_instruct` and `equivalence_tests` grade in Harbor's separate
verifier. When grading fails for a reason the submission can't cause (no system
`python3` in the image, a grader step that can't start, a crashed verifier), the
run writes `reward-details.json` with `"reward": null` and
`"evaluation_status": "ungraded"` and **no reward file**. Harbor then records the
trial as `VERIFIER_FAILED` (`reward_valid` false; its `result.json` shows
`verifier_result: null` and a `RewardFileNotFoundError`), RL consumers drop the
rollout instead of training on a 0, and `harbor run`'s job mean and pass@k still
count it as 0. A failure the submission can cause is a graded 0 instead
(`evaluation_status: "graded"`, reward file written): see the table under
[`pr_runtime`](#pr_runtime--commit_runtime--cve_patches).

---

## `pr_diff`

**Reward kind:** `diff_similarity`

**`reward.txt`:** weighted combination of 6 components, clamped to `[0, 1]`.

**`reward-details.json`:**

```json
{
  "final_reward": 0.74,
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
  "judge_model":  "claude-haiku-4-5-20251001",
  "judge_endpoint": null,
  "judge_status": "ok"
}
```

**Field reference:**

| Field | Type | Description |
|---|---|---|
| `final_reward` | float [0, 1] | Final score, the same value as `reward.txt`. Hard-capped to ≤ 0.40 when `size_sanity < 0.10` (catastrophic-size guard); the breakdown has no separate flag for the cap, so check `components.size_sanity`. |
| `components.format_valid` | 0 or 1 | Predicted output parses as a valid unified diff. Weight 0.00: kept as a guard, not a scoring factor. |
| `components.size_sanity` | [0, 1] | `min(oracle_loc, pred_loc) / max(oracle_loc, pred_loc)`. Catches severe over- or under-generation. |
| `components.file_targeting` | [0, 1] | F1 over the sets of changed files (not Jaccard; F1 gives partial credit for TP). |
| `components.region_overlap` | [0, 1] | Predicted hunks overlap oracle hunks within a 5-line slack. |
| `components.similarity` | [0, 1] | `difflib.SequenceMatcher` ratio over `+`/`-` lines only (no credit for context lines). |
| `components.llm_judge` | [0, 1] or `null` | LLM semantic judge ("does this address the issue?"). `null` on judge failure or when disabled; the evaluation is ungraded and reward is 0. |
| `evaluation_status` | string | `graded` when a positively weighted judge returns a valid score; otherwise `ungraded`. Exclude ungraded runs from training and solved-rate denominators and retry grading the saved patch. |
| `eval_trustworthy` | bool | Whether the configured grading components were available. This is not a claim of functional correctness or tamper isolation. |
| `diagnostic_similarity` | float [0, 1] | Weighted mean of deterministic components, available even for ungraded runs. Diagnostic only. |
| `weights` | object | Effective per-component weights (overridable via `R2E_W_*` env vars). |
| `judge_model` | string or `null` | Model used for `llm_judge` as the serving API names it (e.g. `claude-haiku-4-5-20251001`, `Qwen/Qwen3.5-4B`), or `null` if the judge did not score. |
| `judge_endpoint` | string or `null` | The OpenAI-compatible server the judge was routed to (`R2E_JUDGE_ENDPOINT`), or `null` on the default Anthropic route. |
| `judge_status` | string | `"ok"` \| `"no_api_key"` \| `"no_judge_model"` \| `"empty_predicted"` \| `"network"` \| `"parse"` \| `"missing_score"` \| `"invalid_score"` \| `"disabled"` |

**Weight override env vars** (set inside the verifier container via `--ve`):
`R2E_W_FORMAT`, `R2E_W_SIZE`, `R2E_W_FILE`, `R2E_W_REGION`, `R2E_W_SIM`, `R2E_W_JUDGE`

**Judge routing env vars** (also via `--ve`): `R2E_JUDGE_MODEL`, `R2E_JUDGE_ENDPOINT`, `R2E_JUDGE_API_KEY`. See [`ENV.md`](./ENV.md#pr_diff-reward-tuning).

---

## `pr_runtime` · `commit_runtime` · `cve_patches`

**Reward kind:** `test_execution` (primary) + `diff_similarity` (fallback)

**`reward.txt`:** `f2p_rate × p2p_rate`, rounded to 6 decimal places.

**`reward-details.json` (normal path: test output parsed successfully):**

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

**`reward-details.json` (fallback path: test output unrecognised, no per-test status):**

```json
{
  "reward":            0.0,
  "resolved":          false,
  "command_resolved":  false,
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
| `resolved` | bool | **SWE-bench eval signal.** At least one F2P test is declared, all F2P tests pass AND all P2P tests pass. Gold patch → `true`. Use this for benchmark-style scoring. |
| `command_resolved` | bool | Stricter: `resolved` AND no untracked failures AND `exit_code == 0`. Filters tasks with pre-existing flaky tests outside F2P/P2P. |
| `f2p_total` | int | Number of declared FAIL_TO_PASS tests. |
| `f2p_passed` | int | F2P tests that passed after the agent's patch. |
| `f2p_rate` | float [0, 1] | `f2p_passed / f2p_total`. 0.0 when `f2p_total == 0`. |
| `p2p_total` | int | Number of declared PASS_TO_PASS tests. |
| `p2p_passed` | int | P2P tests still passing after the agent's patch. |
| `p2p_rate` | float [0, 1] | `p2p_passed / p2p_total`. **1.0 when `p2p_total == 0`** (no regression guard). |
| `regressions` | list[str] | P2P tests that broke under the agent's patch. |
| `untracked_failed_count` | int | Tests that failed but were not in F2P or P2P, often from pre-existing flakiness. |
| `untracked_failed` | list[str] | Names of untracked failures (capped at 20). |
| `parse_status` | string | `"ok"`: per-test status parsed successfully. `"fallback_exitcode"`: runner output unrecognised. `reward` is 0 when an F2P oracle exists, otherwise binary and based on the exit code. |
| `eval_trustworthy` | bool | Only present in `fallback_exitcode` path. `false` when an F2P oracle exists but couldn't be verified. |
| `runner` | string | Detected test runner: `"pytest"` \| `"go"` \| `"cargo"` \| `"jest"` \| `""` |
| `tests_parsed` | int | Total tests detected in log output (only in `parse_status == "ok"`). |
| `exit_code` | int | Raw exit code of the test command. |
| `evaluation_status` | string | `"graded"` on every run with a verdict, including the graded 0s below; `"ungraded"` (reward `null`, no reward file) otherwise. |
| `run_seconds` | float | Wall time of the test run, measured by the grader launcher. |
| `tamper_check` | string | The mutant check (see the `pr_runtime` docs): `"passed"`, `"tripped"` (tampered), `"inconclusive"` (`status_mismatch` or `mutant_timeout`), `"not_needed"` (the scored run passed no mutated test, or timed out), `"unavailable"` (the task has no mutant). |
| `tamper_signal` | string | Only when the check fired: `mutant_passed`, `mutant_missing`, `mutant_run_exit_zero`, `status_mismatch` or `mutant_timeout`. |
| `mutant_exit_code`, `mutant_run_seconds`, `mutant_time_ratio` | int, float, float | The run with mutants: exit code, wall time, and its wall time over the scored run's. |

**Grading outcomes before or around the tests.** Each writes `parse_status`,
`resolved: false` and `eval_trustworthy: false`:

| `parse_status` | Verdict | Cause |
|---|---|---|
| `submission_too_large`, `submission_unsafe_symlink`, `submission_invalid`, `submission_apply_failed`, `submission_no_space`, `submission_rebuild_crashed` | graded 0 | The agent's tree could not become a gradable source diff: over 16 MiB or 5,000 files (sizes checked before reading), a symlink resolving outside the repository, a tree the rebuild can't read, a diff that does not apply, a disk filled by the uploaded tree (also while preparing the two runs of the mutant check), or any other failure while the tree is read (an out-of-memory kill included) |
| `test_patch_apply_failed` | graded 0 | The hidden test patch no longer applies over the submission |
| `runner_config_changed`, `test_config_reset_failed` | graded 0 | The submission changed runner configuration the guard can't restore, or made it unreadable |
| `test_timeout` | graded 0 | The test run passed its time limit (`grading_time` in the task metadata) |
| `test_cleanup_failed` | graded 0 | Processes or IPC objects of the test user could not all be removed after the run (in validation, and between the two runs of the mutant check, its files too, within a deadline), or the first run of the mutant check left state the second run's cleanup could not remove |
| `log_unparseable` | graded 0 | The verifier crashed or hit its CPU limit on the output the tests printed |
| `tampered` | graded 0 | The mutant check found a contradiction between the runs (`tamper_signal`): results were forged from inside the test process |
| `status_mismatch` | graded 0 | A tracked test that isn't mutated (a decoy among them) differs between the two runs |
| `mutant_timeout` | graded 0 | The run with mutants passed its time limit after the scored run passed a mutated test |
| `no_system_python`, `submission_rebuild_failed`, `submission_restore_failed`, `grader_launch_failed`, `mutant_setup_failed`, `verifier_error` | ungraded | Grader infrastructure: no image `python3`, grader tools that fail before reading the agent's tree, copying the image's repository (or the prepared tree for the second run) failing or passing its measured limit, a launcher that can't start the tests, hidden test files the grader can't rewrite, an unreadable file the task ships in `/tests` |
| `unsafe_image` | ungraded | The image lets the test user write a path root later imports or executes from (its standard library, the fixed `PATH`, git's exec path, or an ancestor of one): no submission can be graded safely there. Validation drops such tasks, so a shipped task only reaches this if its image was changed |

**Choosing between `reward`, `resolved`, and `command_resolved`:**

- **Training:** use `reward` (dense, graded signal from `reward.txt`).
- **Benchmark / leaderboard:** use `resolved` (strict SWE-bench tracked resolution, unaffected by untracked flakiness).
- **Strict eval (clean command required):** use `command_resolved`.

---

## `code_instruct` · `equivalence_tests`

**Reward kind:** `test_execution`

**`reward.txt`:** `1.0` or `0.0`. Binary.

The verifier runs the hidden pytest file against the submitted
`task_module.py` as an unprivileged user and grades its JUnit report, read only
after every process of the test user is gone. Reward is 1.0 iff pytest exits 0
and the report holds exactly the test IDs the reference solution passed at
generation (`tests/expected.json`), every one passed: a skipped, missing or extra
test fails. Tasks generated with `skip_validation` have no expected list and,
with nothing measured, a 300 s per-run limit; their
report is trusted as is (`eval_trustworthy: false`).

**`reward-details.json`:**

```json
{
  "reward":            1.0,
  "resolved":          true,
  "command_resolved":  true,
  "eval_trustworthy":  true,
  "parse_status":      "ok",
  "tests_expected":    6,
  "tests_parsed":      6,
  "tests_passed":      6,
  "missing":           [],
  "unexpected":        [],
  "not_passed":        [],
  "exit_code":         0,
  "run_seconds":       1.82,
  "evaluation_status": "graded"
}
```

`parse_status` is `"ok"`, `"invalid_report: …"` (no usable report, reward 0),
`"test_timeout"`, or one of the outcomes in the table above
(`submission_not_regular` and `submission_too_large` for the submitted file).
The mutant check runs here too, against the expected test IDs, with the same
`tamper_check` fields.

**Reading the reward for training:**

```python
reward = float(open("/logs/verifier/reward.txt").read().strip())  # 1.0 or 0.0
```

---

## Per-pipeline summary

| Pipeline | `reward.txt` | `reward-details.json` | Training signal | Eval signal |
|---|---|:-:|---|---|
| `pr_diff` | weighted [0,1] | ✅ | `reward` | `components` breakdown |
| `pr_runtime` | f2p × p2p | ✅ | `reward` | `resolved` / `command_resolved` |
| `commit_runtime` | f2p × p2p | ✅ | `reward` | `resolved` / `command_resolved` |
| `cve_patches` | f2p × p2p | ✅ | `reward` | `resolved` / `command_resolved` |
| `code_instruct` | 1.0 / 0.0 | ✅ | `reward` | `reward` |
| `equivalence_tests` | 1.0 / 0.0 | ✅ | `reward` | `reward` |
