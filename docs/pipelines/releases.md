# Harbor dataset release inventory

Snapshot: 2026-09-14T19:39:28.505769+00:00

**1329 generated tasks; 14/15 generation targets reached; 15 datasets published (1329 tasks).**

The target is 100 tasks for twelve owned recipes, 55 TMax tasks, 50 verified Tasksmith tasks, and at least 20 CLI-Gym tasks. CLI-Gym already produced 25; all are retained, so the final inventory is expected to contain **1,330 tasks**. TMax was capped at 55 at the user's request. SEC-bench remains excluded.

Browse the [HuggingEnvs collection](https://huggingface.co/collections/HuggingEnvs/repo2rlenv-verifiable-rl-environments-6aa82300d7494c050f50508d). It also links the six earlier native-pipeline datasets under their existing owners. Those historical collections are outside this new-generation denominator.

## Delivery by method

| Recipe and guide | CLI pipeline / recipe | Generated / target | Publication |
|---|---|---:|---|
| [swe-smith](repo_mutate.md) | `repo_mutate / swe_smith` | 100 / 100 | [Published](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-smith) |
| [r2e](r2e.md) | `equivalence_tests / r2e` | 100 / 100 | [Published](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-r2e) |
| [swe-gen](pr_to_env.md) | `pr_to_env / swe_gen` | 100 / 100 | [Published](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-gen) |
| [swe-next](swe_next.md) | `pr_runtime / swe_next` | 100 / 100 | [Published](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-next) |
| [r2e-gym](r2e_gym.md) | `commit_runtime / r2e_gym` | 100 / 100 | [Published](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-r2e-gym) |
| [scaler](scaler.md) | `reasoning_synth / scaler` | 100 / 100 | [Published](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-scaler) |
| [endless-terminals](endless_terminals.md) | `terminal_synth / endless_terminals` | 100 / 100 | [Published](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-endless-terminals) |
| [cli-gym](env_repair.md) | `env_repair / cli_gym` | 25 / 20 | [Published](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-cli-gym) |
| [swe-flow](repo_reconstruct.md) | `repo_reconstruct / swe_flow` | 100 / 100 | [Published](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-swe-flow) |
| [seta-seed2synth](terminal_synth.md) | `terminal_synth / seta_seed2synth` | 100 / 100 | [Published](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-seta-seed2synth) |
| [seta-evol](task_evolve.md) | `task_evolve / seta_evol` | 100 / 100 | [Published](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-seta-evol) |
| [tmax](tmax.md) | `terminal_synth / tmax` | 55 / 55 | [Published](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-tmax) |
| [terminalworld](terminalworld.md) | `terminal_reconstruct / terminalworld` | 99 / 100 | [Published](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-terminalworld) |
| [dataarc](dataarc.md) | `terminal_synth / dataarc` | 100 / 100 | [Published](https://huggingface.co/datasets/HuggingEnvs/repo2rlenv-dataarc) |
| [tasksmith](tasksmith.md) | `tasksmith run` | 50 / 50 | [Published](https://huggingface.co/datasets/HuggingEnvs/HF_ML_Tasksmith) |

## What the counts establish

Each released task is a Harbor directory with its instruction, configuration, environment, trusted verifier and reference. Archives preserve executable modes. The [release workflow](dataset_release.md) records exact bundle identities, per-file publication audits and commit-pinned registries; the generic tabular Hub viewer is disabled in favor of Harbor Visualiser.

The [public registry and collection audit](evidence/harbor-registry-collection-audit.json) checks membership and artifact revision pins for every published dataset in its snapshot. The [release-file audit](evidence/harbor-release-label-audit.json) checks the uniform-label revisions and subsequent releases against Hub file identities. The [package audit](evidence/owned-package-resources.json) checks that the built wheel includes recipe prompts, data, provenance, license notices and Tasksmith runtime assets.

Generation controls and quality acceptance are separate. New expansion exports have their recipe's native checks and recorded baseline/reference contrast. Historical retained tasks have separate evidence scope. Tasksmith's selected 50 carry verified labels from its audited, assisted campaign; this does not imply unattended acceptance or that every task was solved by Sonnet. Known instruction/verifier defects remain diagnosed and labeled for repair.

## Measured economics

These costs include unsuccessful attempts within the stated campaign. Model-only prices and costs including estimated cloud usage are deliberately identified. Earlier retained-task costs, interactive assistant usage and future independent quality campaigns are excluded unless the row says otherwise.

| Recipe | New exports in cost scope | Recorded USD | USD per new export | Cost scope |
|---|---:|---:|---:|---|
| swe-smith | 76 | $2.41 | $0.03 | Model only; shared cloud costs below |
| r2e | 80 | $17.73 | $0.22 | Model only; shared cloud costs below |
| swe-gen | 80 | $1.63 | $0.02 | Model only; shared cloud costs below |
| swe-next | 80 | $7.51 | $0.09 | Model only; shared cloud costs below |
| r2e-gym | 80 | $3.52 | $0.04 | Model only; shared cloud costs below |
| scaler | 80 | $0.00 | $0.00 | Model only; shared cloud costs below |
| endless-terminals | 80 | $41.51 | $0.52 | Models + estimated cloud/build usage |
| cli-gym | 5 | $5.71 | $1.14 | Models + estimated cloud/build usage |
| swe-flow | 76 | $22.91 | $0.30 | Models + estimated cloud/build usage |
| seta-seed2synth | 77 | $54.64 | $0.71 | Models + estimated cloud/build usage |
| seta-evol | 80 | $44.72 | $0.56 | Models + estimated cloud/build usage; $1.25 remains reserved separately |
| tmax | 35 | $73.51 | $2.10 | Models + estimated cloud/build usage; $5.00 remains reserved separately |
| terminalworld | 79 | $99.44 | $1.26 | Models + estimated cloud/build usage |
| dataarc | 80 | $26.57 | $0.33 | Models + estimated cloud/build usage |
| tasksmith | 26 | $392.01 | $15.08 | Historical expansion including quality, rollouts and estimated compute; not the cost of all 50 |

Wave 1's six recipes share **$43.09** in estimated cloud/build costs, in addition to **$32.81** of model usage: **$75.90 accounted for 476 new exports**, with **$2.25** of uncertain model holds. Count this shared campaign once. Its per-recipe [source and timing reports](economics/wave1/README.md) explain why a zero-model-cost SCALER export is not free compute.

Wave 2's [settled economics](evidence/wave2-generation-economics.json) include all four expansion campaigns. Tasksmith's [retrospective](tasksmith_campaign_retrospective.md) separates investigation, repair, rollout and cloud estimates. Parent allocations and child costs are the same funds; outstanding and uncertain reservations remain separate from actual recorded charges. Cloud estimates are not invoices.

The [machine-readable snapshot](evidence/release-inventory.json) records artifact commits, quality-label distributions and each cost scope. Refresh this page with `python docs/_tools/update_release_inventory.py --releases workspace/owned-releases-100 --expansion workspace/owned-waves34-100`. This reads saved artifacts; it does not dispatch work or establish live provider state.
