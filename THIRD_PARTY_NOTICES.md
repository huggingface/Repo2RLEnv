# Third-party notices

Repo2RLEnv's original code is licensed under [Apache-2.0](LICENSE). The distributed
package also retains adapted code, prompt text and examples under the licenses
listed below. Its combined SPDX expression is **Apache-2.0 AND MIT**. This does
not relicense upstream material or impose that expression on external datasets.

Each retained `UPSTREAM_LICENSE` is shipped beside its recipe and listed in wheel
and source-distribution metadata. Per-recipe provenance identifies retained
material, source paths and our changes. The licenses preserve their original
copyright notices. SCALER's pinned `Notice.txt` is retained as
[`UPSTREAM_NOTICE`](src/repo2rlenv/pipelines/recipes/scaler/UPSTREAM_NOTICE), including
ByteDance attribution, and accompanies its exported verifier code.

| Recipe | Referenced revision | Bundled material license | Scope and changes |
|---|---|---|---|
| `swe_smith` | [9b74ac08118a](https://github.com/SWE-bench/SWE-smith/tree/9b74ac08118a85c39c356802f7961893af73e07f) | MIT | [Provenance](src/repo2rlenv/pipelines/recipes/swe_smith/provenance.md) |
| `seta_seed2synth` | [e4715b01174e](https://github.com/camel-ai/seta/tree/e4715b01174e6c9503fc46120d81dd692ced75e6) | Apache-2.0 | [Provenance](src/repo2rlenv/pipelines/recipes/seta_seed2synth/provenance.md) |
| `seta_evol` | [e4715b01174e](https://github.com/camel-ai/seta/tree/e4715b01174e6c9503fc46120d81dd692ced75e6) | Apache-2.0 | [Provenance](src/repo2rlenv/pipelines/recipes/seta_evol/provenance.md) |
| `swe_gen` | [14e185f413f7](https://github.com/abundant-ai/SWE-gen/tree/14e185f413f7bff03f8f9fec6fb246681bf61d74) | Apache-2.0 | [Provenance](src/repo2rlenv/pipelines/recipes/swe_gen/provenance.md) |
| `swe_flow` | [7da5b046fa1d](https://github.com/Hambaobao/SWE-Flow/tree/7da5b046fa1dc184674e4e94a9989be56c39e4e7) | MIT | [Provenance](src/repo2rlenv/pipelines/recipes/swe_flow/provenance.md) |
| `r2e` | [bcbed156711b](https://github.com/r2e-project/r2e/tree/bcbed156711bb939de14aa46b27eee15073f5272) | MIT | [Provenance](src/repo2rlenv/pipelines/recipes/r2e/provenance.md) |
| `tmax` | [7387d2f91423](https://github.com/hamishivi/tmax/tree/7387d2f9142397a458dc39f0827a2ab0b4c03cda) | Apache-2.0 | [Provenance](src/repo2rlenv/pipelines/recipes/tmax/provenance.md) |
| `terminalworld` | [784698ba9373](https://github.com/EuniAI/TerminalWorld/tree/784698ba93735470ce1664bff2ec44bcd7b28e15) | Apache-2.0 | [Provenance](src/repo2rlenv/pipelines/recipes/terminalworld/provenance.md) |
| `endless_terminals` | [99f4c74b75fa](https://github.com/kanishkg/endless-terminals/tree/99f4c74b75faacf21e53d3dc01df170902e924cb) | Apache-2.0 | [Provenance](src/repo2rlenv/pipelines/recipes/endless_terminals/provenance.md) |
| `cli_gym` | [48bb920b728a](https://github.com/LiberCoders/CLI-Gym/tree/48bb920b728a25a55a5b442303e901919654599e) | MIT | [Provenance](src/repo2rlenv/pipelines/recipes/cli_gym/provenance.md) |
| `dataarc` | [2a1d65ec8dcf](https://github.com/DataArcTech/DataArc-SynData-Toolkit/tree/2a1d65ec8dcfaea2458d67e1fb18078cce6420b9) | Apache-2.0 (our implementation; inspiration only) | [Provenance](src/repo2rlenv/pipelines/recipes/dataarc/provenance.md) |
| `swe_next` | [b55c0841f364](https://github.com/TIGER-AI-Lab/SWE-Next/tree/b55c0841f364f9fe7363b2012cd0ae8d8afdf872) | Apache-2.0 | [Provenance](src/repo2rlenv/pipelines/recipes/swe_next/provenance.md) |
| `r2e_gym` | [0d94c4eb9431](https://github.com/R2E-Gym/R2E-Gym/tree/0d94c4eb9431cd195c55a7ea3abd54006c9a1735) | Apache-2.0 | [Provenance](src/repo2rlenv/pipelines/recipes/r2e_gym/provenance.md) |
| `scaler` | [60c6c5037866](https://github.com/ALEX-nlp/SCALER/tree/60c6c5037866c718f4c001ea338f9c5a91cb01ae) | Apache-2.0 | [Provenance](src/repo2rlenv/pipelines/recipes/scaler/provenance.md) |

## DataArc revision boundary

DataArc recipe version 2 credits the terminal augmentation method but uses
Repo2RLEnv-authored prompt wording and implementation. The referenced terminal
branch does not record a license grant. We removed the earlier copied prompts
and the license obtained from an unrelated main-branch revision. No license is
asserted for that terminal source. Existing version 1 datasets and historical
runs are not relicensed or replaced by this package change.

## Runtime dependencies and input data

Harbor, cloud SDKs, model SDKs, Pi and OpenCode are separately installed dependencies,
not vendored research repositories. Their own licenses apply to their distributions;
this index covers material included in the Repo2RLEnv source and wheel. The pinned
Node lockfile ships, but `node_modules` does not.

Repositories, seeds, recordings, problem families and generated task assets have
separate provenance and terms. Publication or an execution pass does not establish
redistribution permission. Dataset records retain known licensing gaps; consult
those records before reuse. Deferred recipes such as SEC-bench have no executable
implementation or bundled upstream material here.
