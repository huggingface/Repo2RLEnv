# Tasksmith: why the 30-task campaign stalled

September 13, 2026. The [audited blocker inventory](evidence/tasksmith-scale30-blockers.json) records each selected PR, its latest evidence, recovery action and budget snapshot. This audit made no model calls and executed no target code locally.

This is the checkpoint **before** the additional $100 authorization. Recovery is
now running under the $600 total cap; see the
[recovery implementation and execution checkpoint](tasksmith_scale30_recovery_progress.md).
The counts and budget below describe the earlier stopped campaign.

The campaign has **14 accepted tasks: ten retained and four newly accepted**. There are **22 generated Harbor bundles in total**, including eight additional bundles that have not passed acceptance. No campaign jobs are running; the last CPU and GPU workers have terminated and their compute estimates are reconciled.

## Disposition of the twenty selected additions

| Outcome | Count | PRs |
|---|---:|---|
| Accepted | 4 | PEFT #3212; Accelerate #3674; TRL #6228, #6152 |
| Generated, acceptance unfinished | 7 | Transformers #35669; Accelerate #3720, #3142; TRL #6116, #5575, #6206, #6001 |
| Bootstrap or authoring incomplete | 4 | TRL #6150, #5791, #5349; PEFT #3055 |
| Budget denied initial investigation | 2 | Diffusers #13921; Transformers #35348 |
| Held before dispatch | 3 | Transformers #39826; Accelerate #4015, #4022 |

An additional reserve, **TRL #5501**, produced a bundle but exhausted its review allowance. It is the recorded replacement for #6116, whose fixed reference fails its stated idempotence behavior. Including this reserve accounts for the eighth unaccepted bundle. The original #6116 failure remains recorded.

## Causes and required corrections

**Offline readiness was not established reliably before running the bootstrap.** TRL #6150 and #5791 selected upstream tests that fetched Qwen/Llama assets after networking had been disabled. PEFT #3055 also selected a profile requiring model-hub assets. A repository runtime cache does not contain every historical PR's test assets. Each selected test must have a concrete asset plan: pinned files prepared before isolation, or faithful local fixtures exercising the relevant behavior. Keep the final learner and verifier offline.

**Historical dependencies and source packaging consumed repeated attempts.** A working current repository image did not establish compatibility with older Transformers/TRL revisions. Document symlinks also repeatedly interrupted snapshot creation. The implementation now inventories document links before building and preserves native preparation errors; compatible dependency sets and prepared assets still need explicit reuse across PRs.

**Verifier defects were found in successive expensive reviews.** Examples include an unrealizable mesh fixture in Accelerate #3720, missing FSDP integration coverage in #3142, and missing independent SwiGLU coverage in Helium. In TRL #5501, a wrong implementation that logged on worker processes passed the earlier verifier. These are useful findings, but reviewing partial test evidence and discovering one component at a time caused unnecessary iterations. Prepare a complete requirement-to-test map and consolidated repair before rerunning controls. Preserve all discovered wrong and valid implementations. Do not change the fixed reference to satisfy an invalid fixture.

**Some completion limits were infrastructure failures.** TRL #5349 passed bootstrap but its design response was truncated at the provider output limit. TRL #6001's repair request timed out. Use saved source/readiness and completed artifacts, bounded file edits and explicit recovery after uncertain provider outcomes. Neither failure establishes that the PR is unsuitable.

**Reservations and phase limits stopped work that was still repairable.** Several quality runs could not reserve their next trial while a worker reservation was outstanding. A native reservation denial was also mislabeled as an uncertain allocation. That reporting defect is fixed and covered by a test proving no provider dispatch occurs. Active waves used frozen runtimes; the fix does not retroactively complete those attempts. Unknown provider outcomes retain their holds.

The broad Helium architecture task and GPU integration also made this campaign substantially more expensive than the preceding ten-task campaign. The batch continued before those costs and review patterns were sufficiently corrected. The observed yield should not be extrapolated into a claim that the unaccepted PRs cannot become environments.

## Recovery order

| Step | Work | Target total if successful |
|---|---|---:|
| 1 | Recover seven existing drafts: TRL #5501, #5575, #6206, #6001; Accelerate #3720, #3142; Transformers #35669. Preserve each baseline/reference and reuse only matching successful evidence. | 21 |
| 2 | Finish four bootstrap/authoring stops and the two PRs blocked at initial investigation. Fix offline fixture/asset contracts before dispatch; resume #5349 from saved readiness. | 27 |
| 3 | Run the three held GPU PRs, including both two-GPU tasks, with their own numerical/distributed behavior checks. | 30 |

This is a route through the selected inventory, not a guarantee of acceptance. Each failed candidate must retain an explicit diagnosis; any further substitution must be recorded. A passing baseline/reference contrast alone does not replace semantic controls and a reviewed Sonnet rollout.

## Budget constraint

The scaling phase has **$137.82 accounted**: **$60.92** for quality review, repair and probe authoring, **$58.46** for compute including the bootstrap audit, and **$18.44** for author/solver model operations. Compute amounts are conservative estimates, not invoices. This is the scaling phase cost, not a claim that the whole $500 campaign was spent on four tasks.

The full campaign has **$469.29 accounted, $20.09 reserved and $10.62 available**. The nested $145 scaling-phase cap has **$2.63 unreserved** after its own outstanding holds. No additional allowance has been authorized. Finishing sixteen further accepted tasks within those remaining allowances is not supported by the observed costs. Prepare the repairs and demonstrate a cheaper recovery path before committing to another large paid batch.
