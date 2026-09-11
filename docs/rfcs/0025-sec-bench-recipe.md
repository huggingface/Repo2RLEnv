# RFC 0025: `sec_bench` recipe for `cve_patches`

> Deferred at the user's request. SEC-bench is excluded from the current owned
> recipe integration and 20-task generation campaign. The design below is retained
> for future consideration; no implementation or generated-task claim is made.

**Status:** deferred; excluded from the current integration
**Author:** @adithya-s-k
**Created:** 2026-09-11

## Summary

Resolve a vulnerability and known fix, reconstruct the vulnerable build and reproduction input, validate the failure and patched behavior through the security verifier, and emit an isolated repair task with regression checks.

## Motivation

Bring the released method into Repo2RLEnv as owned, maintainable code, with standalone Harbor output and independent quality evidence. The upstream project remains the attribution and comparison baseline, not a runtime dependency. Shared operations use [RFC 0011](0011-owned-recipes.md); method-specific generation decisions stay in this recipe.

## Design

### Input

Native input: Supported documented defects, affected projects and fixed revisions.

Select `pipeline.name: cve_patches` and `pipeline.recipe: sec_bench` in a typed configuration. Source data, resolved revisions, resource limits, model roles, random seeds and recipe options are recorded before spending. Strict options reject unknown keys. Existing native pipeline defaults remain compatible.

### Algorithm

Resolve a vulnerability and known fix, reconstruct the vulnerable build and reproduction input, validate the failure and patched behavior through the security verifier, and emit an isolated repair task with regression checks.

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

Start with supported native inputs. Target 20 generated distinct tasks per recipe first. Expanded quality validation and any 100-task scaling follow only after every recipe reaches its generation milestone. Pilot outputs are insufficient to promise yield. Each recipe guide will report the actually validated domain, sample counts, cost and limitations.

## Dependencies

Repository-owned recipe code, existing source/auth/LLM/bootstrap helpers, remote execution adapters and Harbor. Essential SDKs and ordinary libraries are permitted. No install/import/clone of the upstream research implementation at runtime. No dependency on ignored local reference folders or private pilot artifacts.

## Alternatives considered

A wrapper around upstream commands would preserve an uncontrolled runtime dependency. One generic generator for every method would lose method-specific behavior. Use owned stages with common execution and quality contracts instead; explicitly version deviations from the upstream baseline.

## Rollout plan

Implement the owned algorithm, verify fixture behavior, run remote 1/5/20 waves, complete quality evaluation and publish immutable artifact evidence. Integrate supporting stages where needed. Add the user guide, example configuration, acknowledgments and packaged notices before marking implementation complete.

## Open questions

Exact supported scope, quality yield and cost are implementation evidence to collect. An unresolved runtime or verifier issue blocks an acceptance claim rather than being hidden by a registered CLI command.

## References

- Upstream: [SEC-bench](https://github.com/SEC-bench/SEC-bench)
- Source commit: `31eb43485a3de47da260be0f978528b1f2314415`
- Recorded upstream license: MIT; verify the exact files before adapting them.
- [secb/preprocessor/](https://github.com/SEC-bench/SEC-bench/blob/31eb43485a3de47da260be0f978528b1f2314415/secb/preprocessor/)
- [secb/evaluator/build_eval_instances.py](https://github.com/SEC-bench/SEC-bench/blob/31eb43485a3de47da260be0f978528b1f2314415/secb/evaluator/build_eval_instances.py)
- [secb/evaluator/eval_instances.py](https://github.com/SEC-bench/SEC-bench/blob/31eb43485a3de47da260be0f978528b1f2314415/secb/evaluator/eval_instances.py)

## Implementation

Pending. The integration PR must fill in source, tests, guide and immutable campaign evidence before this status changes.
