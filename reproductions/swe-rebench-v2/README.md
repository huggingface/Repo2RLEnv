# SWE-rebench V2 released components

Status: **planned; no reproduction has run**. Supporting component; target **100 input cases**, not 100 newly synthesized tasks.

[Upstream source](https://github.com/SWE-rebench/SWE-rebench-V2/tree/c71902a8cf8d2b725f63d51f199f4d3e56f68d2d) · MIT · pinned commit `c71902a8cf8d2b725f63d51f199f4d3e56f68d2d`.

## Scope

Reproduce published build/evaluation components; the complete production miner was not located.

**Native input:** Published task records, environment recipes and evaluation inputs.

**Native output:** Installer/parser/annotation components and execution records.

**Harbor boundary:** Import/replay 100 published executable tasks if useful, labeling that campaign as replay rather than synthesis.

**Known limitation:** No invented production miner. Availability of a large dataset is not generation source availability.

## Source entry points

- [prompts/installer/](https://github.com/SWE-rebench/SWE-rebench-V2/tree/c71902a8cf8d2b725f63d51f199f4d3e56f68d2d/prompts/installer)
- [prompts/annotations/](https://github.com/SWE-rebench/SWE-rebench-V2/tree/c71902a8cf8d2b725f63d51f199f4d3e56f68d2d/prompts/annotations)
- [lib/agent/log_parsers.py](https://github.com/SWE-rebench/SWE-rebench-V2/blob/c71902a8cf8d2b725f63d51f199f4d3e56f68d2d/lib/agent/log_parsers.py)

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
