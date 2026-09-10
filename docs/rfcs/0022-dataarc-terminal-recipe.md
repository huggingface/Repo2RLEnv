# RFC 0022: `dataarc` recipe for `terminal_synth`

**Status:** accepted design; implementation pending
**Author:** @adithya-s-k
**Created:** 2026-09-11

## Summary

Use the released terminal synthesis workflow to create an instruction, assets, solution and verifier, followed by execution-informed revision. Limit claims to the released terminal branch and enforce the concrete generated APIs.

## Motivation

Bring the released method into Repo2RLEnv as owned, maintainable code, with standalone Harbor output and independent quality evidence. The upstream project remains the attribution and comparison baseline, not a runtime dependency. Shared operations use [RFC 0011](0011-owned-recipes.md); method-specific generation decisions stay in this recipe.

## Design

### Input

Native input: Upstream seed Harbor tasks and supported few/self/evol settings.

Select `pipeline.name: terminal_synth` and `pipeline.recipe: dataarc` in a typed configuration. Source data, resolved revisions, resource limits, model roles, random seeds and recipe options are recorded before spending. Strict options reject unknown keys. Existing native pipeline defaults remain compatible.

### Algorithm

Use the released terminal synthesis workflow to create an instruction, assets, solution and verifier, followed by execution-informed revision. Limit claims to the released terminal branch and enforce the concrete generated APIs.

Each substantive stage emits a typed progress event and an artifact-bound receipt. Classification separates source eligibility, infrastructure, oracle, verifier and solver failures. Repairs are bounded and invalidate affected downstream evidence.

### Output

A complete Harbor bundle: instruction, task configuration, environment, reference entry point and trusted verifier. Metadata records recipe/version, input lineage, upstream source pin, adaptations, reward scale, image/asset digests and the complete task hash. Exported is distinct from accepted.

## Verification

Require meaningful baseline failure, two fresh reference successes, nonempty expected test/result identities and required passing regressions. Use deterministic behavioral rewards; preserve a native nonzero negative reward where applicable. Independent leakage, partial-solution and shortcut checks plus blind Sonnet/Opus traces determine acceptance. Solver failure alone is not task failure.

## Anti-contamination

Learner-visible snapshots exclude the reference, future Git objects, credentials and private tests. Execute grading so learner code cannot inspect the private oracle. Prefetch pinned assets; enforce and probe the actual learner network policy. The prompt is not an access-control mechanism. Preserve legitimate source context rather than indiscriminately deleting it.

## LLM use

Where the algorithm requires synthesis or review, use recorded role-specific models through the common metered client or agent adapter. Reference execution is deterministic. Cost includes failures, retries, bootstrap, cloud runtime and independent audits; unknown costs are not zero.

## Yield and suitability

Start with supported native inputs. Target 20 accepted distinct tasks, expanding cheap methods to 100 only under the shared budget policy. Pilot outputs are insufficient to promise yield. Each recipe guide will report the actually validated domain, sample counts, cost and limitations.

## Dependencies

Repository-owned recipe code, existing source/auth/LLM/bootstrap helpers, remote execution adapters and Harbor. Essential SDKs and ordinary libraries are permitted. No install/import/clone of the upstream research implementation at runtime. No dependency on ignored local reference folders or private pilot artifacts.

## Alternatives considered

A wrapper around upstream commands would preserve an uncontrolled runtime dependency. One generic generator for every method would lose method-specific behavior. Use owned stages with common execution and quality contracts instead; explicitly version deviations from the upstream baseline.

## Rollout plan

Implement the owned algorithm, verify fixture behavior, run remote 1/5/20 waves, complete quality evaluation and publish immutable artifact evidence. Integrate supporting stages where needed. Add the user guide, example configuration, acknowledgments and packaged notices before marking implementation complete.

## Open questions

Exact supported scope, quality yield and cost are implementation evidence to collect. An unresolved runtime or verifier issue blocks an acceptance claim rather than being hidden by a registered CLI command.

## References

- Upstream: [DataArc terminal synthesis (Envs-FORGE-linked code)](https://github.com/DataArcTech/DataArc-SynData-Toolkit)
- Source commit: `2a1d65ec8dcfaea2458d67e1fb18078cce6420b9`
- Recorded upstream license: Apache-2.0; verify the exact files before adapting them.
- [sdgsystem/agentic_data/terminal_bench.py](https://github.com/DataArcTech/DataArc-SynData-Toolkit/blob/2a1d65ec8dcfaea2458d67e1fb18078cce6420b9/sdgsystem/agentic_data/terminal_bench.py)
- [examples/syn_agentic_data/run_terminal_bench.py](https://github.com/DataArcTech/DataArc-SynData-Toolkit/blob/2a1d65ec8dcfaea2458d67e1fb18078cce6420b9/examples/syn_agentic_data/run_terminal_bench.py)
- [configs/syn_agentic_terminal_bench.yaml](https://github.com/DataArcTech/DataArc-SynData-Toolkit/blob/2a1d65ec8dcfaea2458d67e1fb18078cce6420b9/configs/syn_agentic_terminal_bench.yaml)

## Implementation

Pending. The integration PR must fill in source, tests, guide and immutable campaign evidence before this status changes.
