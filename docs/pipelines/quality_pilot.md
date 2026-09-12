# Quality pilot, scaling decisions and costs

12 September 2026. Reviewer: assistant. Solver: `anthropic/claude-sonnet-4-6`.

**Recommendation:** expand SWE-smith and R2E toward 100 tasks in repository-aware waves. SWE-gen, SWE-Next, R2E-Gym and Endless Terminals are the next promising tracks. Fix diagnosed specification, packaging and verifier defects before expanding the weaker tracks.

## What was actually checked

Pilot02 reviewed five original tasks from each of the remaining twelve active recipes: **60 originals**, **204 Harbor trials**, nine targeted semantic probes, and five immutable CLI-Gym packaging revisions. Execution used Harbor 0.20.0 with Docker inside Modal sandboxes and offline learner containers. Pilot01 separately covered five SWE-smith and five R2E tasks. No 100-task batch was generated during these quality pilots. SEC-bench remains excluded.

All 60 instructions and their verifier/reference structure were reviewed. **60/60** original baseline/reference pairs established the intended contrast. **40/60** selected slots are practically usable after this review, including **5 CLI-Gym revisions**. Counts describe this deliberately small, targeted sample, not population yield. The manifest retains every failed or skipped attempt.

**Across both pilots: 70 original tasks, 246 Harbor trials and 50 practically selected task slots, including seven revisions.** These totals include four reasoning tasks; the other 46 selected tasks are coding or terminal tasks.

The pragmatic bar is a coherent useful request, required offline assets, a working reference and negative control, and checks that assess the main requested behavior. Easy tasks and editorial imperfections can remain usable. A legitimate Sonnet failure does not disqualify a sound task; reward 1 does not establish a sound verifier.

## Pipeline decisions

“Selected” includes reviewed revisions. “Sonnet original” is the raw reward-1 count, including cases whose verifiers were later found weak. CLI-Gym original solvers failed before model execution; all five revised tasks pass. SCALER is a reasoning track, not a repository coding benchmark.

| Recipe | Paired controls | Sonnet original | Selected pilot slots | Next action |
|---|---:|---:|---:|---|
| swe-smith | 5/5 | 5/5 | 5/5 | Scale in stages across new repositories. |
| swe-gen | 5/5 | 5/5 | 5/5 | Scale in stages; avoid patch-revealing prompts and duplicate PRs. |
| swe-flow | 5/5 | 4/5 | 3/5 | Repair specification inference before a broad batch. |
| swe-next | 5/5 | 4/5 | 5/5 | Scale in stages; retain regression protection even when Sonnet fails. |
| r2e | 5/5 | 5/5 | 5/5 | Scale in stages, carrying forward the two pilot01 repairs. |
| r2e-gym | 5/5 | 5/5 | 5/5 | Scale in stages; broaden beyond small guided more-itertools fixes. |
| seta-seed2synth | 5/5 | 4/5 | 3/5 | Repair script execution and ambiguous metrics; retain the useful subset. |
| seta-evol | 5/5 | 5/5 | 3/5 | Keep the simple usable subset; strengthen reusable CSV/log behavior checks. |
| endless-terminals | 5/5 | 5/5 | 5/5 | Scale as a basic terminal-workflow track, with varied task families. |
| tmax | 5/5 | 4/5 | 1/5 | Repair semantic execution checks before scaling. |
| terminalworld | 5/5 | 4/5 | 1/5 | Hold: missing assets, exposed answers and weak behavioral checks. |
| cli-gym | 5/5 | 0/5 | 5/5 | Use fixed packaging; diversify projects and disruption classes before 100. |
| dataarc | 5/5 | 5/5 | 0/5 | Hold: protect verifier inputs/helpers; calibrate optimization resources. |
| scaler | 5/5 | 4/5 | 4/5 | Separate reasoning track; repair typed/tolerant grading first. |

## Findings that change the decision

- **Sound learner failure:** SWE-Next’s empty-split task rejected an extra unintended `split_at` change. Its reference passes; keep the task and its regression test.
- **Prompt/test mismatch:** SWE-Flow’s `difference` prompt describes the wrong `initial` behavior. Its interleave task leaves the general scheduling rule unclear; Sonnet timed out without replacing the source stub. SETA’s URL task penalizes an undefined token-count convention.
- **False positives:** a failing monitoring script, no-op ShowVars, dead archive/sensor scripts, changed calendar input and copy-only log report each earned reward 1 in a targeted probe.
- **False negatives:** TMax rejected numeric time span `20` versus `20.0` without requiring that distinction publicly. SCALER rejected a rounded numeric answer within its stated relative tolerance.
- **Missing assets and leakage:** the TerminalWorld crackme contains a placeholder instead of the promised ELF and exposes the password in a learner-visible test. Its reference fabricates a substitute. That is not a valid reverse-engineering task.
- **Execution limits differ from wrong answers:** DataArc’s C optimization agent timed out while running additional local benchmarks, but its submitted code passed all seven private checks. Its mutable baseline remains a separate verifier repair. The reference itself passes within the configured resources.
- **Observed transcript shortcut:** after attempting real JShell capture, Sonnet replaced the requested output with a handwritten transcript and received reward 1. The crackme learner also read exposed answers and fabricated output, though that trial received reward 0.
- **Packaging repaired:** CLI-Gym now preinstalls `tmux`; original images could pass nop/oracle but could not start an offline Terminus-2 solver. All five revised tasks have fresh nop 0, oracle 1 and Sonnet 1. Two misleading instruction details were also corrected.
- **Audit correction:** SETA-Evol’s normalization tool restriction is explicitly public. An earlier static note was wrong and was corrected after reading the full task. That sample is usable with notes.

### Targeted counterexamples

These probe-only variants preserve the original instruction, environment and verifier. Their private delivery script first creates a correct result, then installs a deliberately wrong submission or a valid alternative. They are not ordinary reference-solution acceptance runs.

| Probe | Expected | Observed | Interpretation |
|---|---:|---:|---|
| changed-calendar | 0 | 1 | False positive |
| noop-makefile | 0 | 0 | Correct rejection |
| valid-rounded-answer | 1 | -1 | False negative |
| fixed-report | 0 | 1 | False positive |
| failing-monitor | 0 | 1 | False positive |
| noop-showvars | 0 | 1 | False positive |
| dead-sensor-scripts-with-import | 0 | 1 | False positive |
| dead-sensor-scripts | 0 | 0 | Rejected by a superficial import/definition keyword check; refined probe passes |
| dead-archive-script | 0 | 1 | False positive |

The no-op Makefile is rejected because that verifier actually cleans and invokes the targets. The refined sensor script includes an `import` statement, then raises immediately; this bypasses the initial superficial check and still earns reward 1. Numeric Rings evidence: reference `1632.34573381740718411859`, submitted `1632.345735`, relative error approximately `7.24e-10`, public tolerance `1e-9`.

## Cost to generate and check tasks

Generation API estimates include recorded failed/retried calls across all historical campaign waves, with unresolved generation reservations kept in the estimate. Historical shared generation compute of **$31.10** is allocated equally across 291 exports (**$0.107/export**). This allocation is a planning proxy, not measured recipe-specific compute. The table does not divide by the tiny pilot acceptance fraction.

| Recipe | Exports | Authoring API calls | API/export | Generation/export incl. allocated compute | Generate 100 | Add to existing corpus to reach 100 | Mean Sonnet API/check |
|---|---:|---:|---:|---:|---:|---:|---:|
| swe-smith | 24 | 28 | $0.027 | $0.134 | $13.38 | $10.17 | $0.068 |
| swe-gen | 20 | 20 | $0.019 | $0.126 | $12.59 | $10.07 | $0.138 |
| swe-flow | 24 | 48 | $0.047 | $0.153 | $15.34 | $11.66 | $0.274 |
| swe-next | 20 | 20 | $0.061 | $0.168 | $16.79 | $13.43 | $0.082 |
| r2e | 20 | 47 | $0.073 | $0.180 | $17.99 | $14.39 | $0.162 |
| r2e-gym | 20 | 20 | $0.066 | $0.172 | $17.24 | $13.79 | $0.076 |
| seta-seed2synth | 23 | 131 | $0.913 | $1.020 | $101.96 | $78.51 | $0.076 |
| seta-evol | 20 | 68 | $0.489 | $0.595 | $59.55 | $47.64 | $0.045 |
| endless-terminals | 20 | 105 | $0.538 | $0.645 | $64.51 | $51.61 | $0.063 |
| tmax | 20 | 150 | $1.127 | $1.234 | $123.38 | $98.71 | $0.086 |
| terminalworld | 20 | 239 | $0.678 | $0.785 | $78.51 | $62.81 | $0.065 |
| cli-gym | 20 | 175 | $1.199 | $1.306 | $130.63 | $104.50 | $0.058 |
| dataarc | 20 | 63 | $0.927 | $1.034 | $103.43 | $82.74 | $0.141 |
| scaler | 20 | 0 | $0.000 | $0.107 | $10.69 | $8.55 | $0.076 |

Nominal generation alone is **$766.00 for 100 tasks in each of all fourteen tracks**, or **$608.59 to add to the existing 291 exports**. This excludes future quality checks, repairs and new repository bootstraps. Existing exports have not all passed quality review, so topping up export counts does not guarantee 100 usable tasks.

For a rough reviewed-batch budget, use `task count × (generation/export + mean Sonnet API/check + $0.101 allocated pilot compute/check)`, then add a 30% contingency and explicit new-repository bootstrap allowance. Timeout trials can have incomplete usage, retained as separate budget reservations. The compute proxy includes pilot probes and repair overhead; do not treat it as a provider invoice or a guarantee. It does not price open-ended repair of the weak tracks.

| Promising first batch | Planning cost for 100 generated and individually checked tasks, with 30% contingency |
|---|---:|
| swe-smith | $39, plus new repository/bootstrap costs |
| swe-gen | $47, plus new repository/bootstrap costs |
| swe-next | $46, plus new repository/bootstrap costs |
| r2e | $58, plus new repository/bootstrap costs |
| r2e-gym | $45, plus new repository/bootstrap costs |
| endless-terminals | $105, plus new repository/bootstrap costs |

Pilot02 accounting: **$4.61 API**, **$6.55 estimated compute/build allowance**, **$11.16 total accounted**. Remaining campaign allowance after all current holds: **$293.71**. The original $500 authorization accounts for $22.43 outside this ledger; the ledger limit is $477.57. Uncertain historical reservations remain reserved.

Pilot02 also retains **$12.00** in unresolved API reservations for incomplete usage, separate from accounted spend. All pilot workers were terminated and compute was reconciled. A revision worker was stopped after a stale-wheel preflight rejection, before any trial; its small compute estimate and conservative $1 build allowance are retained. Five original CLI-Gym API holds were released only after receipts showed agent setup failed before any agent execution; their cloud time remains accounted.

The three timeout receipts report at least **$1.28** in model usage within those holds. Including that partial usage, the observed pilot cost is at least **$12.44**. The $12 reservation is not a claim of $12 charged, and should not be added again to the partial usage.

Rate-card verification, 12 September 2026: Sonnet 4.6 is $3/input and $15/output per million tokens, while Opus 4.6 is $5/$25; cache rates differ. [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing). Modal Sandbox rates used for conservative estimates are $0.00003942/CPU-core-second and $0.00000667/GiB-second. [Modal pricing](https://modal.com/pricing). Daytona lists $0.0504/vCPU-hour, $0.0162/GiB-hour, and storage beyond the first 5 GiB at $0.000108/GiB-hour. [Daytona pricing](https://www.daytona.io/pricing). These are list-price estimates, excluding assistant development/review usage, credits, taxes and negotiated pricing.

## How to scale without amplifying defects

1. Take SWE-smith and R2E to a ten-task expansion pilot across at least two new CPU Python repositories, using the corrected task policies. Then fill toward 100 after inspecting that wave.
2. Add SWE-gen, SWE-Next and R2E-Gym with PR/commit and semantic deduplication; add Endless Terminals as a clearly labeled basic terminal track.
3. Use the repaired CLI-Gym images and broaden beyond pytest/import corruption before filling 100. Fix the specific output-regeneration, protected-input and specification defects in other tracks before launching another five-task quality sample.
4. Track generation success, packaging/reference validity, practical usability, difficulty, diversity and total cost separately. Proposed diversity targets: at most 25 tasks per repository and 10 near-identical task variants in a 100-task release. These are future targets, not retroactive acceptance gates.

## Tasksmith: the main change

Keep LangGraph and the Pi/OpenCode coding-agent boundary. Reuse the existing bootstrap cache, paired controls, requirement contracts and durable evidence. Route a PR to the smallest faithful construction strategy: existing PR regression tests first, changed-function reconstruction when appropriate, or terminal/environment repair for workflow changes. A PR should not silently become an unrelated random mutation.

The highest-value loop is **verified merged behavior → human request → executable verifier → plausible wrong solution and valid alternative → blind Sonnet review → targeted repair**. Require real assets, clear offline testing instructions, fresh execution for reusable scripts, protected expected data, and a reward adapter matching the output type. Most observed defects need a small concrete fix; they do not justify replacing the whole harness or requiring every task to be solved by a stronger model.

See [Tasksmith implementation recommendations](tasksmith_pilot_learnings.md) for the actual old-code audit, routing diagram and stage questions.

## Evidence availability and limits

The detailed local evidence is retained under `workspace/owned-campaign/quality-pilot-01/` and `quality-pilot-02/`. These directories are ignored by Git and are not included in this PR. The second directory contains `QUALITY_REPORT.md`, `quality-report.json`, `manifest.json`, `preferred-tasks.json`, per-task judgments, trial results, trajectories, semantic probes, immutable revisions and budget receipts.

All 291 original export hashes were verified unchanged. Repository tasks retain submitted source diffs; terminal tasks generally declare no final artifact collection, so their complete final filesystems were not independently captured. Five targeted examples per recipe and one solver seed do not establish population yield, cross-repository reliability, training gains or comprehensive reward-hack resistance. These findings concern our current owned reproductions, not a claim about all implementations of the upstream methods.
