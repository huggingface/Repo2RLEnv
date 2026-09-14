# Published Harbor datasets

Results cover the six native pipelines, Tasksmith and all 14 research recipes. Historical evidence and the newer publication checks are reported separately below.

Browse the [HuggingEnvs collection](https://huggingface.co/collections/HuggingEnvs/repo2rlenv-verifiable-rl-environments-6aa82300d7494c050f50508d). The native datasets retain their existing owners.

## Native pipelines

**600 task entries across 6 earlier datasets.** Recovered from cached Hub manifests and local publication stagings on **2026-09-15**. These are historical snapshots, not a fresh Hub recount. Older revisions and duplicate stagings are excluded; cross-pipeline content is not deduplicated.

| Pipeline | Tasks | Recovered validation evidence | Dataset and evidence |
|---|---:|---|---|
| [pr_diff](pr_diff.md) | 181 | 181 listed; no per-task validation in this snapshot. Earlier 100-task release reported oracle passes. | [Dataset](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-diff) · [Manifest](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-diff/resolve/46562965fb50991185a1df142fbf8a48e33f6feb/manifest.json) |
| [pr_runtime](pr_runtime.md) | 100 | 100 oracle/tracked passes; 88 clean commands; 87 with a regression guard. | [Dataset](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-runtime) · [Manifest](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-pr-runtime/resolve/2e7b836c8e1450d12535f7cd598a4705d51fceac/manifest.json) |
| [commit_runtime](commit_runtime.md) | 100 | 100 generation-time verified stamps. Full 100-task oracle gate not recovered; older 52-task gate is separate. | [Dataset](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-commit-runtime) · [Manifest](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-commit-runtime-v2/resolve/84cfe388c88594380902d6e86e31a066f461437c/manifest.json) |
| [code_instruct](code_instruct.md) | 100 | 100 emitted after generation checks; no separate 100-task Harbor gate recovered. | [Dataset](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-code-instruct) · [Local evidence](native_results.md#evidence-and-reproduction) |
| [equivalence_tests](equivalence_tests.md) | 100 | 100 emitted after generation checks; no separate 100-task Harbor gate recovered. | [Dataset](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-equivalence-tests) · [Local evidence](native_results.md#evidence-and-reproduction) |
| [cve_patches](cve_patches.md) | 19 | 19 listed; validation fields absent. A 19-task oracle or independent quality gate was not recovered. | [Dataset](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-cve-patches) · [Manifest](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-cve-patches/resolve/bf9df9dcaf5bc9e77136140c3a60aa2348131c59/manifest.json) |

These tasks have **historical evidence scopes**, not retrospectively assigned `verified` labels. In particular, the earlier 52-task commit-runtime gate does not validate the later 100-task dataset. [Read the native results and solver samples](native_results.md).

## Tasksmith and research recipes

**1,330 tasks across 15 datasets.** Each dataset contains complete Harbor task directories, archives and a registry pinned to its artifact revision.

| Pipeline | Tasks | Evaluation labels | Dataset and pinned manifest |
|---|---:|---|---|
| [swe-smith](repo_mutate.md) | 100 | 100 unverified | [Dataset](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-smith) · [Manifest](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-smith/resolve/70449d9e6e0cfb10fd40c5ed0ad13649a4f23393/manifest.json) |
| [r2e](r2e.md) | 100 | 100 unverified | [Dataset](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-r2e) · [Manifest](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-r2e/resolve/3371888a7fbc24f777e012b266ddfc64860f1970/manifest.json) |
| [swe-gen](pr_to_env.md) | 100 | 100 unverified | [Dataset](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-gen) · [Manifest](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-gen/resolve/46f70216c01db67966cb5f8dc58f83357f5cf1c7/manifest.json) |
| [swe-next](swe_next.md) | 100 | 100 unverified | [Dataset](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-next) · [Manifest](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-next/resolve/917f48135e7368c42b962860c2475975d0b87b12/manifest.json) |
| [r2e-gym](r2e_gym.md) | 100 | 100 unverified | [Dataset](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-r2e-gym) · [Manifest](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-r2e-gym/resolve/2f7877053cb5dc13205b19bb972356c8cb0d5587/manifest.json) |
| [scaler](scaler.md) | 100 | 100 unverified | [Dataset](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-scaler) · [Manifest](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-scaler/resolve/f5596a59fda8e0a129600a0283a4695c4089d3aa/manifest.json) |
| [endless-terminals](endless_terminals.md) | 100 | 100 unverified | [Dataset](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-endless-terminals) · [Manifest](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-endless-terminals/resolve/80adde1459f297bac72cf95467d28a2c064231ee/manifest.json) |
| [cli-gym](env_repair.md) | 25 | 25 unverified | [Dataset](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-cli-gym) · [Manifest](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-cli-gym/resolve/cd2c470fe2a8f285f1689d70bef4ed70e9972a1d/manifest.json) |
| [swe-flow](repo_reconstruct.md) | 100 | 2 needs repair; 98 unverified | [Dataset](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-flow) · [Manifest](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-flow/resolve/b128e025e07bc2e2cd5b74f32accb02b04552b8a/manifest.json) |
| [seta-seed2synth](terminal_synth.md) | 100 | 100 unverified | [Dataset](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-seta-seed2synth) · [Manifest](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-seta-seed2synth/resolve/b754de9fb40fd0f9e0ea6222fa850273b20f61ff/manifest.json) |
| [seta-evol](task_evolve.md) | 100 | 100 unverified | [Dataset](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-seta-evol) · [Manifest](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-seta-evol/resolve/eb7b37ad9703bd79357fb078991fb606029d07ed/manifest.json) |
| [tmax](tmax.md) | 55 | 55 unverified | [Dataset](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-tmax) · [Manifest](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-tmax/resolve/bfb4c83ebf4e9f932169f1b55c507ce11a97e0cc/manifest.json) |
| [terminalworld](terminalworld.md) | 100 | 3 needs repair; 97 unverified | [Dataset](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-terminalworld) · [Manifest](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-terminalworld/resolve/9e6460a5e2a3bfe5a59b87512c3df95117160667/manifest.json) |
| [dataarc](dataarc.md) | 100 | 100 unverified | [Dataset](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-dataarc) · [Manifest](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-dataarc/resolve/eec33b1d7cafd420a2ad05eacabc2023e85bdd6c/manifest.json) |
| [tasksmith](tasksmith.md) | 50 | 50 verified | [Dataset](https://huggingface.co/datasets/HuggingEnvs/HF_ML_Tasksmith) · [Manifest](https://huggingface.co/datasets/HuggingEnvs/HF_ML_Tasksmith/resolve/2961be38506e8990656a67a21b9d8d5dd6606ec5/manifest.json) |

## What the labels establish

The Tasksmith and research-recipe release contains **50 verified, 5 needing repair and 1,275 unverified** tasks. These totals exclude the historical native inventories above. Tasksmith's verified cohort came from an assisted campaign; this does not claim unattended conversion. Two SWE-flow instruction issues and three TerminalWorld verifier gaps remain explicitly diagnosed. Each dataset manifest supplies task-level labels, diagnostics and evidence scope.

Publication checks for those 15 datasets compared 214,097 file identities and parsed every selected task with Harbor. This establishes artifact integrity and format, not semantic quality of every task. See [evaluation labels](task_evaluation_labels.md), [yield and cost](economics.md), and [how to publish](dataset_release.md).
