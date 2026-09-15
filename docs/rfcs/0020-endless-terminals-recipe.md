# RFC 0020: `endless_terminals` recipe for `terminal_synth`

**Status:** experimental implementation in [PR #109](https://github.com/huggingface/Repo2RLEnv/pull/109); release evidence below
**Author:** @adithya-s-k
**Created:** 2026-09-11

## Summary

Generate terminal task descriptions and container definitions with the method-specific prompt stages, build the environment, create solutions and tests, and convert into Harbor. Record conversion changes and repair demonstrated verifier shortcuts as owned improvements.

## Motivation

Bring the released method into Repo2RLEnv as owned, maintainable code, with standalone Harbor output and independent quality evidence. The upstream project remains the attribution and comparison baseline, not a runtime dependency. Shared operations use [RFC 0011](0011-owned-recipes.md); method-specific generation decisions stay in this recipe.

## Design

### Input

Native input: Its original task-generation settings, prompts and configured model.

Select `pipeline.name: terminal_synth` and `pipeline.recipe: endless_terminals` in a typed configuration. Source data, resolved revisions, resource limits, model roles, random seeds and recipe options are recorded before spending. Strict options reject unknown keys. Existing native pipeline defaults remain compatible.

### Algorithm

Generate terminal task descriptions and container definitions with the method-specific prompt stages, build the environment, create solutions and tests, and convert into Harbor. Record conversion changes and repair demonstrated verifier shortcuts as owned improvements.

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

- Upstream: [Endless Terminals](https://github.com/kanishkg/endless-terminals)
- Source commit: `99f4c74b75faacf21e53d3dc01df170902e924cb`
- Recorded upstream license: Apache-2.0; verify the exact files before adapting them.
- [generate_tasks.py](https://github.com/kanishkg/endless-terminals/blob/99f4c74b75faacf21e53d3dc01df170902e924cb/generate_tasks.py)
- [generator/apptainer_def_gen.py](https://github.com/kanishkg/endless-terminals/blob/99f4c74b75faacf21e53d3dc01df170902e924cb/generator/apptainer_def_gen.py)
- [generator/convert_to_harbor/convert_sif_docker.py](https://github.com/kanishkg/endless-terminals/blob/99f4c74b75faacf21e53d3dc01df170902e924cb/generator/convert_to_harbor/convert_sif_docker.py)
- [generator/convert_to_harbor/add_reward_file.py](https://github.com/kanishkg/endless-terminals/blob/99f4c74b75faacf21e53d3dc01df170902e924cb/generator/convert_to_harbor/add_reward_file.py)

## Implementation

The owned sampler and retained prompts live in `recipes/endless_terminals/`.
Shared terminal stages execute the distinct initial-state and completion-test
authors, fixture preflight and Harbor baseline/reference pair. The
[guide](../pipelines/endless_terminals.md) and example configuration expose
the supported profile. Contract tests check seeded sampling and stage evidence
flow. The twenty-task generation campaign precedes sampled-solution evaluation.

## Current release evidence

[100 published Harbor tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-endless-terminals). The [pipeline walkthrough](../pipelines/endless_terminals.md) documents the implemented profile, actual model calls, bounded repairs and limitations. The [release inventory](../pipelines/releases.md) records source diversity, scoped economics, artifact revisions and evaluation labels. Generation checks, independent review and blind solver success are separate claims.
