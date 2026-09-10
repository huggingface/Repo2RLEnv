# Reproduction campaign status

Snapshot: September 10, 2026 UTC. Branch `codex/upstream-reproductions`, draft PR #101.

**28 distinct Harbor environments: 25 coding/terminal and three algorithmic
reasoning instances. 21 pass reference/no-op execution contrast, including two
explicit repairs from the first batch. Zero have completed training approval.**
CLI-Gym's two instruction variants share one environment and count once here.
Do not treat these totals as an acceptance yield: projects use different input
units, filters, native checks and pilot sizes.

| Generator | Distinct exports | Contrast passes | Current result |
| --- | ---: | ---: | --- |
| SETA Seed2Synth | 3 | 3 with one repair | First pilot saved and published privately |
| SETA Evol | 2 | 2 | Two native context-evolution tasks |
| Endless Terminals | 5 | 1 | Quality failures documented, including reward shortcuts |
| TMax | 1 | 1 with one repair | Missing verifier dependency fixed in a separate variant |
| SWE-smith | 5 | 5 | Procedural mutations in one supported repository |
| CLI-Gym | 1 | 0 | Native empty-test-result reward bug; no restoration oracle |
| SWE-Flow | 4 | 3 | Fourth reference has a syntax error; native/Harbor agreement on all four |
| TerminalWorld | 1 | 1 | Native refinement and three partial checks pass; final export rebuilt and verified |
| SWE-gen | 1 | 1 | Native Axios HTTP/2 PR-to-Harbor run passes |
| SWE-Next | 0 | 0 | Three emitted rows have no positive test contrast |
| R2E-Gym | 0 | 0 | Eligible Pyramid change fails native test collection |
| R2E | 1 | 1 | One function with seven original generated tests |
| SEC-bench | 0 | 0 | Instance image built; incompatible with SecVerifier runtime |
| DataArc terminal | 1 | 0 | Static acceptance, broken reference API and missing output |
| SCALER | 3 | 3 | Released-family reasoning expansion; native -1/+1 rewards retained |

Execution contrast means doing nothing fails and the supplied reference succeeds.
It does not establish task difficulty, adequate coverage, absence of leakage or
resistance to reward hacking. The first pilot has two successful offline blind
Sonnet samples. The expanded batch still needs independent solver and adversarial
checks. TerminalWorld's native task currently permits internet.

## Experimental runner

`run.py` exposes each implemented native stage under one interface, with remote
execution, bounded stage times, budget reservations, duplicate-attempt exclusion,
dependency receipts and recipe hashes. It leaves original agents and prompts in
each upstream implementation. Recipes currently describe bounded pilot inputs;
they are not a general arbitrary-PR service or the production repo2rlenv CLI.

The core bootstrap runs without private pilot artifacts. `runtime/restore-pilot`
is an explicit additional dependency for the CLI-Gym and harden-v0 examples.
Nine local controller/export tests pass; they do not start containers or models.
Task execution and image construction run on Modal.

## Components and outstanding work

- SWE-Flow-Trace executed as part of SWE-Flow; it creates no additional tasks.
- harden-v0 ran one native iteration. The fixer judged the attempt legitimate and
  made no change; the known counterfeit-Python shortcut remains unresolved.
- SecVerifier is installed. Our credential type mismatch was repaired; the
  published SEC-bench base image still lacks its required OpenHands runtime.
- SWE-rebench V2 ran its original two annotation templates and passed golden
  evaluation on one published netCDF-C sample (12 F2P, 177 P2P). Harbor replay
  packaging remains pending; this is not fresh task synthesis.
- RepoLaunch, SWE-bench-Live, SWE-Dev and SWE-Mutation have pinned source folders,
  but their runtime pilots and unified-runner recipes are still outstanding.
  RepoLaunch also expects a Tavily key, which is absent from the current `.env`.

Next: resolve the runtime failures or record bounded upstream blockers, finish
the component pilots, run independent rollout/verifier audits, and repeat on
broader inputs. The **100-per-generator target remains outstanding**. Tasksmith
has not been rewritten; proposed improvements are in [TASKSMITH_LESSONS.md](TASKSMITH_LESSONS.md).

Artifacts and costs are recorded separately. The first batch has an immutable
HF receipt in [artifacts.json](artifacts.json); the expanded batch is recorded in
[artifacts-phase02.json](artifacts-phase02.json). Its final snapshot contains
3,980 verified files, including historical source uploads and final TerminalWorld
trials. Private artifact access is required for saved traces.

Both remote workers are terminated. The reconciled campaign accounts for
**$22.43**, leaving **$477.57** of the $500 allowance, with no open reservations.
These are model reports and conservative rate/cloud estimates, not invoices;
see [cost-summary-phase02.json](cost-summary-phase02.json).

Validation: nine reproduction-specific controller/export tests pass, repository
lint/format checks pass, and all 51 new shell recipes pass syntax checks. The
full repository test run reports 716 passed, four skipped, and one failed live
GitHub test: its two currently sampled PRs were filtered as test-only/too-large,
so its assertion that at least one task is emitted failed. Production pipeline
code was not changed in this reproduction batch.
