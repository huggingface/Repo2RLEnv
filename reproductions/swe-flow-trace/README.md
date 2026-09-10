# SWE-Flow-Trace

Status: **planned; no reproduction has run**. Supporting component; target **100 input cases**, not 100 newly synthesized tasks.

[Upstream source](https://github.com/Hambaobao/SWE-Flow-Trace/tree/e7251448ae7d29c7de5fdcda2a3bc175e547420e) · MIT · pinned commit `e7251448ae7d29c7de5fdcda2a3bc175e547420e`.

## Scope

A dependency of the SWE-Flow reproduction, with its own source pin.

**Native input:** Testable Python repositories.

**Native output:** Runtime dependency traces.

**Harbor boundary:** Trace data supports SWE-Flow task construction; it is not independently a Harbor task.

**Known limitation:** Record unsupported tracing modes and missing calls.

## Source entry points

- [sweflow_trace/python/](https://github.com/Hambaobao/SWE-Flow-Trace/tree/e7251448ae7d29c7de5fdcda2a3bc175e547420e/sweflow_trace/python)

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
