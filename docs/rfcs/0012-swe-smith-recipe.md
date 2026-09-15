# RFC 0012: `swe_smith` recipe for `repo_mutate`

**Status:** experimental implementation in [PR #109](https://github.com/huggingface/Repo2RLEnv/pull/109); release evidence below
**Author:** @adithya-s-k
**Created:** 2026-09-11

## Summary

Apply bounded procedural mutations to source entities in a healthy repository. Validate candidate defects against its existing tests, recover fail-to-pass and pass-to-pass identities, then write a symptom-based issue. The pristine implementation supplies the restoration oracle.

## Motivation

Bring the released method into Repo2RLEnv as owned, maintainable code, with standalone Harbor output and independent quality evidence. The upstream project remains the attribution and comparison baseline, not a runtime dependency. Shared operations use [RFC 0011](0011-owned-recipes.md); method-specific generation decisions stay in this recipe.

## Design

### Input

Native input: A supported repository profile and healthy upstream image.

Select `pipeline.name: repo_mutate` and `pipeline.recipe: swe_smith` in a typed configuration. Source data, resolved revisions, resource limits, model roles, random seeds and recipe options are recorded before spending. Strict options reject unknown keys. Existing native pipeline defaults remain compatible.

### Algorithm

Apply bounded procedural mutations to source entities in a healthy repository. Validate candidate defects against its existing tests, recover fail-to-pass and pass-to-pass identities, then write a symptom-based issue. The pristine implementation supplies the restoration oracle.

Each substantive stage emits a typed progress event and an artifact-bound receipt. Classification separates source eligibility, infrastructure, oracle, verifier and solver failures. Repairs are bounded and invalidate affected downstream evidence.

### Output

A complete Harbor bundle: instruction, task configuration, environment, reference entry point and trusted verifier. Metadata records recipe/version, input lineage, upstream source pin, adaptations, reward scale, image/asset digests and the complete task hash. Exported is distinct from accepted.

## Verification

The linked pipeline guide specifies the implemented native generation checks.
Generation exports and independent quality acceptance are separate: reference
success does not establish verifier coverage or shortcut resistance. Shared
review, repair and labeling contracts are defined in [RFC 0027](0027-harbor-quality-loop.md).

## Anti-contamination

Learner-visible snapshots exclude the reference, future Git objects, credentials and private tests. Execute grading so learner code cannot inspect the private oracle. Prefetch pinned assets; enforce and probe the actual learner network policy. The prompt is not an access-control mechanism. Preserve legitimate source context rather than indiscriminately deleting it.

## LLM use

Where the algorithm requires synthesis or review, use recorded role-specific models through the common metered client or agent adapter. Reference execution is deterministic. Cost includes failures, retries, bootstrap, cloud runtime and independent audits; unknown costs are not zero.

## Yield and suitability

See the pipeline guide for supported inputs and [measured economics](../pipelines/economics.md)
for sample sizes, yield definitions and cost coverage. Results on a selected source
profile do not imply universal input conversion.

## Dependencies

Repository-owned recipe code, existing source/auth/LLM/bootstrap helpers, remote execution adapters and Harbor. Essential SDKs and ordinary libraries are permitted. No install/import/clone of the upstream research implementation at runtime. No dependency on ignored local reference folders or private pilot artifacts.

## Alternatives considered

A wrapper around upstream commands would preserve an uncontrolled runtime dependency. One generic generator for every method would lose method-specific behavior. Use owned stages with common execution and quality contracts instead; explicitly version deviations from the upstream baseline.



## References

- Upstream: [SWE-smith](https://github.com/SWE-bench/SWE-smith)
- Source commit: `9b74ac08118a85c39c356802f7961893af73e07f`
- Recorded upstream license: MIT; verify the exact files before adapting them.
- [scripts/bug_gen_modal.py](https://github.com/SWE-bench/SWE-smith/blob/9b74ac08118a85c39c356802f7961893af73e07f/scripts/bug_gen_modal.py)
- [swesmith/bug_gen/](https://github.com/SWE-bench/SWE-smith/blob/9b74ac08118a85c39c356802f7961893af73e07f/swesmith/bug_gen/)
- [swesmith/issue_gen/](https://github.com/SWE-bench/SWE-smith/blob/9b74ac08118a85c39c356802f7961893af73e07f/swesmith/issue_gen/)
- [swesmith/harness/valid.py](https://github.com/SWE-bench/SWE-smith/blob/9b74ac08118a85c39c356802f7961893af73e07f/swesmith/harness/valid.py)

## Implementation

Owned implementation: `pipelines/repo_mutate.py` and `pipelines/recipes/swe_smith/`.
Options use the standard registry; prompts and MIT notices are packaged. The
[guide](../pipelines/repo_mutate.md) documents supported profiles, CLI and recovery.
Controller tests and a remote Harbor nop/oracle/oracle pilot establish execution
contrast. Independent quality acceptance and the 20/100-task campaign remain open.

## Current release evidence

[100 published Harbor tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-smith). The [pipeline walkthrough](../pipelines/repo_mutate.md) documents the implemented profile, actual model calls, bounded repairs and limitations. The [release inventory](../pipelines/releases.md) records source diversity, scoped economics, artifact revisions and evaluation labels. Generation checks, independent review and blind solver success are separate claims.
