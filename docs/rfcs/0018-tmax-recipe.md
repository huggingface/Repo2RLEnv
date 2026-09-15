# RFC 0018: `tmax` recipe for `terminal_synth`

**Status:** experimental implementation in [PR #109](https://github.com/huggingface/Repo2RLEnv/pull/109); release evidence below
**Author:** @adithya-s-k
**Created:** 2026-09-11

## Summary

Sample a task domain and requirements, construct fixtures and a container environment, author the task and executable tests, run reference/solver feedback, and export the resulting task. Preserve the method-specific sampler and generator stages rather than aliasing SETA.

## Motivation

Bring the released method into Repo2RLEnv as owned, maintainable code, with standalone Harbor output and independent quality evidence. The upstream project remains the attribution and comparison baseline, not a runtime dependency. Shared operations use [RFC 0011](0011-owned-recipes.md); method-specific generation decisions stay in this recipe.

## Design

### Input

Native input: Upstream domain/skill/fixture sampling configuration and base-image recipes.

Select `pipeline.name: terminal_synth` and `pipeline.recipe: tmax` in a typed configuration. Source data, resolved revisions, resource limits, model roles, random seeds and recipe options are recorded before spending. Strict options reject unknown keys. Existing native pipeline defaults remain compatible.

### Algorithm

Sample a task domain and requirements, construct fixtures and a container environment, author the task and executable tests, run reference/solver feedback, and export the resulting task. Preserve the method-specific sampler and generator stages rather than aliasing SETA.

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

- Upstream: [TMax](https://github.com/hamishivi/tmax)
- Source commit: `7387d2f9142397a458dc39f0827a2ab0b4c03cda`
- Recorded upstream license: Apache-2.0; verify the exact files before adapting them.
- [rl_data/generate_tasks.py](https://github.com/hamishivi/tmax/blob/7387d2f9142397a458dc39f0827a2ab0b4c03cda/rl_data/generate_tasks.py)
- [rl_data/generator/](https://github.com/hamishivi/tmax/blob/7387d2f9142397a458dc39f0827a2ab0b4c03cda/rl_data/generator/)
- [rl_data/containers/](https://github.com/hamishivi/tmax/blob/7387d2f9142397a458dc39f0827a2ab0b4c03cda/rl_data/containers/)
- [rl_data/scripts/analyze/convert_to_harbor.py](https://github.com/hamishivi/tmax/blob/7387d2f9142397a458dc39f0827a2ab0b4c03cda/rl_data/scripts/analyze/convert_to_harbor.py)

## Implementation

The owned sampler, template, initial/final test authors and fixture preflight
live in `pipelines/recipes/tmax/`. Remote execution uses the shared terminal
materializer with a non-root solver. [The guide](../pipelines/tmax.md) and
`examples/owned-tmax.yaml` describe the supported legacy profile. Contract tests
cover seeded sampling, conditional domains/languages and the Harbor user/private
artifact configuration. Generation is running toward twenty tasks; the v2
multimodal and sampled-solution stages are explicitly deferred.

## Current release evidence

[55 published Harbor tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-tmax). The [pipeline walkthrough](../pipelines/tmax.md) documents the implemented profile, actual model calls, bounded repairs and limitations. The [release inventory](../pipelines/releases.md) records source diversity, scoped economics, artifact revisions and evaluation labels. Generation checks, independent review and blind solver success are separate claims.
