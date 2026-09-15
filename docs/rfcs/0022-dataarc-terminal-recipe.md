# RFC 0022: `dataarc` recipe for `terminal_synth`

**Status:** experimental implementation in [PR #109](https://github.com/huggingface/Repo2RLEnv/pull/109); release evidence below
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

- Upstream: [DataArc terminal synthesis (Envs-FORGE-linked code)](https://github.com/DataArcTech/DataArc-SynData-Toolkit)
- Source commit: `2a1d65ec8dcfaea2458d67e1fb18078cce6420b9`
- Referenced terminal revision: no recorded license grant. Version 2 retains method credit only and uses Repo2RLEnv-authored prompts and implementation under Apache-2.0.
- [sdgsystem/agentic_data/terminal_bench.py](https://github.com/DataArcTech/DataArc-SynData-Toolkit/blob/2a1d65ec8dcfaea2458d67e1fb18078cce6420b9/sdgsystem/agentic_data/terminal_bench.py)
- [examples/syn_agentic_data/run_terminal_bench.py](https://github.com/DataArcTech/DataArc-SynData-Toolkit/blob/2a1d65ec8dcfaea2458d67e1fb18078cce6420b9/examples/syn_agentic_data/run_terminal_bench.py)
- [configs/syn_agentic_terminal_bench.yaml](https://github.com/DataArcTech/DataArc-SynData-Toolkit/blob/2a1d65ec8dcfaea2458d67e1fb18078cce6420b9/configs/syn_agentic_terminal_bench.yaml)

## Implementation

Owned implementation: `pipelines/recipes/dataarc/`. Version 2's authored prompt and
four strategy instructions feed direct artifact generation over complete Harbor seeds. Typed
materialization, full environment context, remote baseline/reference checks and
bounded repairs are explicit adaptations. Tests cover strategy enumeration,
context filtering, source identity and missing-reference rejection. See the
[guide](../pipelines/dataarc.md) and `examples/owned-dataarc.yaml`. Quality
acceptance remains deferred until the generation milestone across all recipes.

Version 1 retained prompt text from the terminal branch and a license from main,
which does not contain that implementation. Version 2 removes both retained
artifacts. New exports record the recipe revision; runtime-wheel identity prevents
resuming an old run with changed prompt resources. Published version 1 tasks are
historical results, not evidence of version 2 generation quality or licensing.

## Current release evidence

[100 published Harbor tasks](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-dataarc). The [pipeline walkthrough](../pipelines/dataarc.md) documents the implemented profile, actual model calls, bounded repairs and limitations. The [release inventory](../pipelines/releases.md) records source diversity, scoped economics, artifact revisions and evaluation labels. Generation checks, independent review and blind solver success are separate claims.
