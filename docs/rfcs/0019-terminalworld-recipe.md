# RFC 0019: `terminalworld` recipe for `terminal_reconstruct`

**Status:** experimental implementation in [PR #109](https://github.com/huggingface/Repo2RLEnv/pull/109); release evidence below
**Author:** @adithya-s-k
**Created:** 2026-09-11

## Summary

Filter a supplied terminal recording, infer its initial state and intended outcome, reconstruct the environment, and generate a reference and tests. Refine against execution and partial-solution probes without exposing the recorded solution.

## Motivation

Bring the released method into Repo2RLEnv as owned, maintainable code, with standalone Harbor output and independent quality evidence. The upstream project remains the attribution and comparison baseline, not a runtime dependency. Shared operations use [RFC 0011](0011-owned-recipes.md); method-specific generation decisions stay in this recipe.

## Design

### Input

Native input: Asciinema recordings and associated metadata accepted by the upstream filters.

Optional owned discovery reads bounded public/recent/featured/popular explore
pages, retaining numeric recording IDs and hashed page receipts. It checks
robots.txt, reuses cached pages, and stops on empty/repeated pages. Text acquisition
and privacy/feasibility filtering remain separate stages. This follows the native
source discovery approach without consuming an upstream dataset's task answers.

An owned extension accepts explicit public Asciinema profile paths under the same
bounded discovery contract, with at most 200 pages across feeds and profiles.
Profile selection must be recorded as source curation, not described as random
sampling. Acquisition records its running/interrupted/completed state and binds
completion to the retrieval receipt hash, so batch scheduling can distinguish
partially downloaded sources from a completed final shard.

Select `pipeline.name: terminal_reconstruct` and `pipeline.recipe: terminalworld` in a typed configuration. Source data, resolved revisions, resource limits, model roles, random seeds and recipe options are recorded before spending. Strict options reject unknown keys. Existing native pipeline defaults remain compatible.

### Algorithm

Filter a supplied terminal recording, infer its initial state and intended outcome, reconstruct the environment, and generate a reference and tests. Refine against execution and partial-solution probes without exposing the recorded solution.

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

- Upstream: [TerminalWorld (EuniAI)](https://github.com/EuniAI/TerminalWorld)
- Source commit: `784698ba93735470ce1664bff2ec44bcd7b28e15`
- Recorded upstream license: Apache-2.0; verify the exact files before adapting them.
- [data_retrieval/](https://github.com/EuniAI/TerminalWorld/blob/784698ba93735470ce1664bff2ec44bcd7b28e15/data_retrieval/)
- [data_filtering/](https://github.com/EuniAI/TerminalWorld/blob/784698ba93735470ce1664bff2ec44bcd7b28e15/data_filtering/)
- [task_synthesis/](https://github.com/EuniAI/TerminalWorld/blob/784698ba93735470ce1664bff2ec44bcd7b28e15/task_synthesis/)
- [environment_building/](https://github.com/EuniAI/TerminalWorld/blob/784698ba93735470ce1664bff2ec44bcd7b28e15/environment_building/)
- [test_generation/](https://github.com/EuniAI/TerminalWorld/blob/784698ba93735470ce1664bff2ec44bcd7b28e15/test_generation/)

## Implementation

The owned implementation is in `recipes/terminalworld/`: public text acquisition,
retained transcript screening and scoring prompts, distinct solution/instruction
authors, a remote reconstruction/replay worker, and snapshot-informed test
generation. [The guide](../pipelines/terminalworld.md) and example configuration
describe the supported runtime and recorded adaptations. Unit contracts cover
screening boundaries, metadata extraction and filesystem changes. The campaign
targets twenty generated tasks before detailed native partial and quality trials.

## Current release evidence

[100 published Harbor tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-terminalworld), meeting the generation target. The [pipeline walkthrough](../pipelines/terminalworld.md) documents the implemented profile, actual model calls, bounded repairs and limitations, including one assisted recovery and three diagnosed verifier gaps. The [release inventory](../pipelines/releases.md) records source diversity, scoped economics, artifact revisions and evaluation labels. Generation checks, independent review and blind solver success are separate claims.
