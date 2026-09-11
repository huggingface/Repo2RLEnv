# SETA evolution attribution

Adapted from [camel-ai/SETA](https://github.com/camel-ai/seta), Apache-2.0,
commit `e4715b01174e6c9503fc46120d81dd692ced75e6`.

`evolution_prompt.md`, `builder_prompt.md` and all six files in `strategies/`
retain the respective files from `datasynth/evol_pipeline/agents/`. The algorithm
mapping is `datasynth/evol_pipeline/evol_task_pipeline.py`: load complete parent
task, select explicit evolution strategy, design variants, optionally filter,
materialize, execute, repair and retain self-review.

Owned adaptations replace filesystem agent actions with typed model responses
and controlled materialization. Parent inputs currently require owned, integrity-
checked Harbor text bundles no larger than 150 kB. The same offline CPU profile
and deterministic JUnit runner as the SETA seed recipe apply. The upstream
design prompt suggests one to four tests while its builder requires five to ten;
this implementation follows the builder's five-to-ten-test contract.

Strategy names are preserved. Presumed 8B-model success rates in the upstream
slight-adjustment prompts are explicitly treated as unmeasured here. No difficulty
calibration or independent quality acceptance is inferred from author self-review.
No upstream research package is installed or imported at runtime.

The owned design response has a 9,000-token ceiling and an explicit 18,000-character draft limit. Design sections describe the build without embedding complete implementation files; the artifact author writes those separately. This bounds truncation seen in the generation campaign.
