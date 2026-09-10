# RFC 0021: `cli_gym` recipe for `env_repair`

**Status:** accepted design; implementation pending
**Author:** @adithya-s-k
**Created:** 2026-09-11

## Summary

Start with a healthy development workspace and its nonempty test identities. Apply a reproducible environment-breaking change, construct a restoration instruction, and retain a working setup restoration oracle. Grade against trusted original tests.

## Motivation

Bring the released method into Repo2RLEnv as owned, maintainable code, with standalone Harbor output and independent quality evidence. The upstream project remains the attribution and comparison baseline, not a runtime dependency. Shared operations use [RFC 0011](0011-owned-recipes.md); method-specific generation decisions stay in this recipe.

## Design

### Input

Native input: Healthy SWE-smith repository images and upstream destruction settings.

Select `pipeline.name: env_repair` and `pipeline.recipe: cli_gym` in a typed configuration. Source data, resolved revisions, resource limits, model roles, random seeds and recipe options are recorded before spending. Strict options reject unknown keys. Existing native pipeline defaults remain compatible.

### Algorithm

Start with a healthy development workspace and its nonempty test identities. Apply a reproducible environment-breaking change, construct a restoration instruction, and retain a working setup restoration oracle. Grade against trusted original tests.

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

- Upstream: [CLI-Gym](https://github.com/LiberCoders/CLI-Gym)
- Source commit: `48bb920b728a25a55a5b442303e901919654599e`
- Recorded upstream license: MIT; verify the exact files before adapting them.
- [src/cli_gym/cli.py](https://github.com/LiberCoders/CLI-Gym/blob/48bb920b728a25a55a5b442303e901919654599e/src/cli_gym/cli.py)
- [src/cli_gym/build_destruction_task/](https://github.com/LiberCoders/CLI-Gym/blob/48bb920b728a25a55a5b442303e901919654599e/src/cli_gym/build_destruction_task/)
- [src/cli_gym/assemble_problem_instance/](https://github.com/LiberCoders/CLI-Gym/blob/48bb920b728a25a55a5b442303e901919654599e/src/cli_gym/assemble_problem_instance/)
- [src/cli_gym/utils/docker_utils.py](https://github.com/LiberCoders/CLI-Gym/blob/48bb920b728a25a55a5b442303e901919654599e/src/cli_gym/utils/docker_utils.py)

## Implementation

Pending. The integration PR must fill in source, tests, guide and immutable campaign evidence before this status changes.
