# R2E

Status: **planned; no reproduction has run**. Position 12 in the generation queue; target **100 Harbor task exports**.

[Upstream source](https://github.com/r2e-project/r2e/tree/bcbed156711bb939de14aa46b27eee15073f5272) · MIT · pinned commit `bcbed156711bb939de14aa46b27eee15073f5272`.

## Scope

Reproduce the narrower function-level task unit after repository-level workflows.

**Native input:** Functions from repositories accepted by the upstream extraction and execution tooling.

**Native output:** Function/specification records, generated execution tests and reference behavior.

**Harbor boundary:** Wrap the upstream function reconstruction/equivalence task in a small workspace; preserve its tested input domain.

**Known limitation:** A reference-execution result is not the specification of a novel feature. This is a function-task reproduction.

## Source entry points

- [src/r2e/pat/dependency_slicer/](https://github.com/r2e-project/r2e/tree/bcbed156711bb939de14aa46b27eee15073f5272/src/r2e/pat/dependency_slicer)
- [src/r2e/generators/specgen/](https://github.com/r2e-project/r2e/tree/bcbed156711bb939de14aa46b27eee15073f5272/src/r2e/generators/specgen)
- [src/r2e/generators/testgen/genexec.py](https://github.com/r2e-project/r2e/blob/bcbed156711bb939de14aa46b27eee15073f5272/src/r2e/generators/testgen/genexec.py)

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
