# SETA Evol

Status: **planned; no reproduction has run**. Position 2 in the generation queue; target **100 Harbor task exports**.

[Upstream source](https://github.com/camel-ai/seta/tree/e4715b01174e6c9503fc46120d81dd692ced75e6) · Apache-2.0 · pinned commit `e4715b01174e6c9503fc46120d81dd692ced75e6`.

## Scope

Reuse the SETA installation and runtime; change only to its published evolution path.

**Native input:** Existing Harbor tasks accepted by the upstream evolution configuration; prefer its published seeds first.

**Native output:** Evolved Harbor tasks and lineage/authoring artifacts.

**Harbor boundary:** Keep native task bundles and retain parent identifiers; do not add a new curriculum scheduler.

**Known limitation:** Configured evolution is not a learned scheduler. Keep children separate from originals and preserve the upstream strategy.

## Source entry points

- [datasynth/evol_pipeline/run_evol_orchestrator.py](https://github.com/camel-ai/seta/blob/e4715b01174e6c9503fc46120d81dd692ced75e6/datasynth/evol_pipeline/run_evol_orchestrator.py)
- [datasynth/evol_pipeline/evol_task_pipeline.py](https://github.com/camel-ai/seta/blob/e4715b01174e6c9503fc46120d81dd692ced75e6/datasynth/evol_pipeline/evol_task_pipeline.py)
- [datasynth/evol_pipeline/agents/evol_strategy_prompts/](https://github.com/camel-ai/seta/tree/e4715b01174e6c9503fc46120d81dd692ced75e6/datasynth/evol_pipeline/agents/evol_strategy_prompts)

## Reproduction steps

1. Check out the pinned upstream source into an isolated environment and record the exact environment, model and native command.
2. Run the original workflow remotely on one native input. Preserve all original outputs and logs.
3. Reach five outputs and verify native execution plus Harbor export parity. Record every compatibility patch separately.
4. Run a 20-input/task pilot as applicable, account for failed attempts and estimate the remaining cost.
5. Scale sequentially toward the target under the existing spending limit; retain every result and independent audit outcome.

See [the common protocol](../PROTOCOL.md) for counting, fidelity, validation and artifact rules. `experiment.json` is a planning/status record, not a runnable upstream configuration. No new Tasksmith stages, prompt rewrites or model substitutions are implicit.

## Files for this experiment

- `experiment.json`: source pin, queue position, target and status.
- `README.md`: scope, references and reproduction notes.
- `config/`: exact upstream configurations once prepared; no credentials.
- `patches/`: minimal compatibility patches with an explanation of any effect on semantics.
- `runs/`: ignored native outputs, logs, costs and evidence per run.
- `harbor/`: ignored exported task copies, separate from native outputs.
- `RESULTS.md`: measured summary and durable artifact references, written after a run.

The future config, patch, run and result files are created only when the corresponding work is done.
