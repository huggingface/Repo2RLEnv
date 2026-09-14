# Tasksmith: five-PR CPU pilot

The fixed panel produced **five Harbor tasks from five PRs**, and all five reached `usable` under `practical-generation-v1`. Each final task rejected its defective starting implementation, accepted the actual PR reference, rejected a plausible wrong implementation, accepted a valid alternative, and recorded a legitimate Sonnet success. The learner starting source and private oracle stayed unchanged throughout quality repairs.

These are useful, small bug-fix tasks. This result establishes a working pipeline for this panel; it does not establish universal conversion yield or difficult training examples. No PR was replaced to improve the denominator.

Read the [pipeline and exact prompt guide](tasksmith.md) for implementation, [RFC 0028](../rfcs/0028-tasksmith-pr-pilot.md) for scope, and the [machine-readable evidence summary](evidence/tasksmith-cpu-hf-pilot.json) for source pins, bundle hashes and final trial receipts.

## Final outcomes

Recorded on September 13, 2026. Scores are reviewer assessments from 0 to 4; execution controls determine rewards separately.

| PR and task | Initial fail-to-pass / pass-to-pass tests | Final required tests | Repairs in final run | Task / verifier / leakage scores | Sonnet reward |
|---|---:|---:|---:|---|---:|
| [more-itertools #1193](https://github.com/more-itertools/more-itertools/pull/1193): empty `interleave_evenly` input | 1 / 10 | 11 | 0* | 3 / 4 / 4 | 1.0 |
| [more-itertools #1251](https://github.com/more-itertools/more-itertools/pull/1251): `value_chain` exception propagation | 1 / 6 | 8 | 1 | 4 / 4 / 4 | 1.0 |
| [huggingface_hub #1316](https://github.com/huggingface/huggingface_hub/pull/1316): nested cache references | 1 / 7 | 9 | 1 | 4 / 3 / 4 | 1.0 |
| [smolagents #1003](https://github.com/huggingface/smolagents/pull/1003): JSON-string tool arguments and escaped quotes | 1 / 17 | 19 | 1 | 4 / 3 / 4 | 1.0 |
| [Click #3865](https://github.com/pallets/click/pull/3865): short-help abbreviations | 6 / 30 | 36 | 0 | 4 / 3 / 4 | 1.0 |

*The first task inherited a repaired revision from the preceding run. Zero repairs here does not mean its original draft needed no repair. Initial contrast counts describe construction evidence; final test counts include later quality repairs. Focused test selections, rather than complete repository suites, determine reward.*

All five final baselines scored **0.0** and all five references scored **1.0**. There are **26 final native trial receipts**: five baselines, five references, six wrong implementations, five valid alternatives and five learner rollouts. Earlier attempts and reviews remain in their original run directories; they are not counted as additional accepted tasks.

## What the verifier controls establish

| Task | Wrong implementation rejected with reward 0 | Valid alternative accepted with reward 1 |
|---|---|---|
| Empty interleaving | Returning early on any empty `lengths` bypasses count validation; materializing all results breaks incremental consumption | Check emptiness after ordering lengths while preserving validation and laziness |
| Exception propagation | Materializing the iterator consumes future items and raises errors too early | Delegate iterable classification to a helper while yielding lazily |
| Nested cache references | Return the first cached snapshot instead of the snapshot named by the requested reference | Resolve reference paths with `pathlib` |
| JSON arguments | Leave encoded tool arguments as a string | Decode with an equivalent `JSONDecoder` call |
| Short-help abbreviations | Treat only uppercase following words as sentence boundaries, missing digits | An independent implementation of whole-word shortening |

These are behavioral controls, not comparisons against the reference patch. A wrong probe must fail for the intended semantic reason, rather than a setup error. An accepted alternative demonstrates some implementation freedom. Neither proves that every wrong implementation is rejected.

The reviewer uses the private original PR intent and diff to check generated requirements. The coding agent sees the public instruction and source workspace. Hidden tests and references live outside its image; submitted allowlisted source files are graded in a separate offline container. Starting source is the PR head with only the source patch reversed, recorded as `head_minus_source_patch`; it is not an exact checkout of the PR base.

## Repairs and pipeline lessons

The final run reused three existing generations and generated Smolagents and Click afresh. Instructions and tests were edited by the automated review/repair loop; there were no manual task-file edits. Pipeline defects were fixed between runs, so this is an iterative development result rather than an untouched first-attempt success rate.

| Observation | Implemented response | Evidence in this panel |
|---|---|---|
| A merged repository built, but excluding its README broke the public learner image | Bootstrap also builds the filtered learner workspace before design | Public build failures are caught before quality review or rollout |
| Authors exposed implementation steps in instructions | Review identifies fix prescriptions and repairs the request while preserving the oracle | Value-chain and Smolagents requests were repaired; diagnostic context may remain |
| A weak cache test accepted the wrong snapshot | Keep discovered counterexamples and add independent exact-path, multi-snapshot assertions | The previously successful wrong probe now scores 0 |
| Checking generator type missed eager consumption | Require observable consumption and exception-timing assertions | Both iterator tasks reject materialization |
| Review demanded probes before reading requested code, or exhausted slots without a valid alternative | Serve reads first, retain existing probes and reserve space for both control kinds | Every final task has wrong and valid controls |
| Large repositories produced repetitive inventory context | Keep full hashes locally, select relevant code and send compact path/size inventories to the reviewer | HF tasks completed review within bounded budgets |
| A bootstrap retry spent its calls rediscovering an existing profile | Include the previous profile and reserve the last two calls for submission/correction | Fresh Smolagents bootstrap completed in the final run |
| The Click draft invented requirements inconsistent with the fixed PR | Give the reviewer original PR intent privately and regenerate reference-conflicted drafts | Corrected framing passed with the original oracle unchanged |

Dependency image reuse is implemented within a shared worker, but the final run recorded **zero named-prefix cache hits**. It reused three complete generations, and its two fresh repositories needed different dependency recipes. Even the two `more-itertools` PRs required different Flit version ranges. This pilot does not demonstrate a persistent cross-campaign cache.

## Models, execution and checks

- Author: Pi with `anthropic/claude-sonnet-4-6`.
- Independent review and repair: `openai/gpt-6-astra`.
- Learner: `anthropic/claude-sonnet-4-6`, one attempt per final revision.
- Provider: Modal. All target builds, imports, tests and rollouts ran remotely.
- Runtime: Harbor **0.20.0**, task schema **1.3**, and the owned `OfflineDockerEnvironment`. The adapter enforces permanent Docker network isolation because Harbor's dynamic firewall needs `nft_fib`, unavailable in the tested Modal kernel.
- Source commit: `a716d3d9be45412a6a72da87bc0af379aae5bcc1`. Runtime wheel SHA-256: `8bea2724e23d4ff5ec4ebeacce2ddeac80e1b4f5ccb9199668823f202c8fa612`.

Daytona and OpenCode remain selectable through the same contracts. This live panel exercised Modal and Pi, not every provider/runtime combination. Twenty-five real Pi/OpenCode SDK contract tests ran against local mock servers. The code suite passed 898 tests locally, with three skips: unavailable optional `pyflakes` and `libcst` suites, and the opt-in live GitLab test. Target/live-provider suites were excluded from that local run. CI also passed Python 3.12/3.13/3.14, optional extras, runtime contracts, lint and distribution builds for the recorded source commit. [Code CI results](https://github.com/huggingface/Repo2RLEnv/actions/runs/34716895651).

## Cost accounting

Amounts below are usage-based model estimates plus conservative compute estimates, **not reconciled provider invoices**. Interrupted model calls keep their reservations. Worker compute uses the full allocated CPU/memory rate for the receipt's lifetime, doubled for conservatism, plus a $0.50 image-build allowance per worker. Rates used were $0.00003942 per physical CPU core-second and $0.00000667 per GiB-second. [Modal pricing](https://modal.com/pricing).

| Run | Accounted estimate | Of which compute | Unresolved model reservation |
|---|---:|---:|---:|
| v1 | $1.159531 | $0.561241 | $0.138465 |
| v2 | $2.330708 | $0.757045 | $0.654912 |
| v3 | $12.226557 | $0.988070 | $0 |
| v4, completed pilot | $9.396530 | $1.124180 | $0 |
| **All Tasksmith attempts** | **$25.113326** | **$3.430536** | **$0.793377** |

The final run's model estimate was $8.272350: $0.542217 for new authoring and $7.730133 for the quality loops, including their rollouts and repairs. Per-task quality estimates were $0.674102, $1.517503, $2.566503, $1.634942 and $1.337083 in table order. They exclude shared compute and earlier generation. Because three generations were reused, dividing the final run by five is **not a from-scratch generation price**.

The original $500 reproduction campaign has $22.43 of prior spend outside the shared ledger. At this snapshot, the ledger accounts for $208.712939 and reserves $15.543377, leaving **$253.313684** available. Its effective cap remains $477.57; it was not reset. All Tasksmith workers are terminated. The $0.793377 Tasksmith reservation covers two interrupted model calls with uncertain final usage; the remaining reservations belong to earlier campaign work.

## Artifacts and remaining limitations

The delivery archive is `workspace/tasksmith-cpu-hf-pilot.tar.gz`. It contains five final task directories, a manifest, the original quality results and portable copies of 26 final trial records, grading output, available trajectories and submitted source artifacts. The [committed evidence summary](evidence/tasksmith-cpu-hf-pilot.json) records the archive checksum and exact task identities without local machine paths. Full upstream source bundles and traces are kept in the artifact archive rather than duplicated in the Git diff. Review records preserve their original paths; the manifest supplies relative paths after extraction.

The final audit recomputed bundle/result hashes, compared all protected learner source and oracle files against construction artifacts, and scanned packaged files for configured credential values. These checks establish artifact integrity and the stated privacy boundary, not a general security proof.

The final reviewer retained several nonblocking improvements: the first task's grading description suggests a full suite instead of selected tests; the HF cache task could cover the cached-nonexistence sentinel; Smolagents could combine quoted strings and literal backslashes inside encoded arguments; Click could cover Unicode lowercase and punctuation-starting words. Some instructions could also be shorter. Scores reflect these gaps, and `usable` should not be read as perfect coverage.

The next useful scale experiment is a new frozen panel of similarly small CPU Python PRs, tracking first-attempt yield, repaired yield, per-stage cost and cache hits separately. The current implementation explicitly excludes GPU, multi-service, non-Python and added/deleted source-file changes. Expanding those profiles should be a separate change with its own execution evidence.
