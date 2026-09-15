# RFC 0026: `scaler` recipe for `reasoning_synth`

**Status:** experimental implementation in [PR #109](https://github.com/huggingface/Repo2RLEnv/pull/109); release evidence below
**Author:** @adithya-s-k
**Created:** 2026-09-11

## Summary

Load a released problem family containing generator and reference programs, sample reproducible difficulty/seed inputs, compute reference answers remotely, and package concrete reasoning tasks with their native reward scale. New-family synthesis is not implied.

## Motivation

Bring the released method into Repo2RLEnv as owned, maintainable code, with standalone Harbor output and independent quality evidence. The upstream project remains the attribution and comparison baseline, not a runtime dependency. Shared operations use [RFC 0011](0011-owned-recipes.md); method-specific generation decisions stay in this recipe.

## Design

### Input

Native input: Upstream seed problems and parameterized environment-generation configuration.

Select `pipeline.name: reasoning_synth` and `pipeline.recipe: scaler` in a typed configuration. Source data, resolved revisions, resource limits, model roles, random seeds and recipe options are recorded before spending. Strict options reject unknown keys. Existing native pipeline defaults remain compatible.

### Algorithm

Load a released problem family containing generator and reference programs, sample reproducible difficulty/seed inputs, compute reference answers remotely, and package concrete reasoning tasks with their native reward scale. New-family synthesis is not implied.

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

- Upstream: [SCALER](https://github.com/ALEX-nlp/SCALER)
- Source commit: `60c6c5037866c718f4c001ea338f9c5a91cb01ae`
- Recorded upstream license: Apache-2.0; verify the exact files before adapting them.
- [SCALER/api_generate_generator_for_environment.py](https://github.com/ALEX-nlp/SCALER/blob/60c6c5037866c718f4c001ea338f9c5a91cb01ae/SCALER/api_generate_generator_for_environment.py)
- [SCALER/generate_problem_from_environment.py](https://github.com/ALEX-nlp/SCALER/blob/60c6c5037866c718f4c001ea338f9c5a91cb01ae/SCALER/generate_problem_from_environment.py)
- [recipe/environment/](https://github.com/ALEX-nlp/SCALER/blob/60c6c5037866c718f4c001ea338f9c5a91cb01ae/recipe/environment/)

## Implementation

Owned implementation: `recipes/scaler/`. Released family expansion uses remote
Python/C++ execution, native scaling and problem framing, and the active upstream
math-verify reward route. See the [guide](../pipelines/scaler.md), example config,
packaged provenance and `tests/test_scaler_recipe.py`. New-family synthesis and
training are outside this profile; quality acceptance remains deferred.

## Current release evidence

[100 published Harbor tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-scaler). The [pipeline walkthrough](../pipelines/scaler.md) documents the implemented profile, actual model calls, bounded repairs and limitations. The [release inventory](../pipelines/releases.md) records source diversity, scoped economics, artifact revisions and evaluation labels. Generation checks, independent review and blind solver success are separate claims.
