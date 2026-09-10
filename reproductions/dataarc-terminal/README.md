Current execution results: [RESULTS.md](RESULTS.md). Runnable stages: `python reproductions/run.py plan dataarc-terminal`.

# DataArc terminal synthesis (Envs-FORGE-linked code)

Status: **planned; no reproduction has run**. Position 14 in the generation queue; target **100 Harbor task exports**.

[Upstream source](https://github.com/DataArcTech/DataArc-SynData-Toolkit/tree/2a1d65ec8dcfaea2458d67e1fb18078cce6420b9) · Apache-2.0 · pinned commit `2a1d65ec8dcfaea2458d67e1fb18078cce6420b9`.

## Scope

Reproduce the available branch after core synthesis/evolution baselines, with explicit partial-release scope.

**Native input:** Upstream seed Harbor tasks and supported few/self/evol settings.

**Native output:** Harbor task bundles and synthesis manifests.

**Harbor boundary:** Keep the existing Harbor materializer and verification entry points.

**Known limitation:** This reproduces released toolkit functionality. The paper's MILP/frontier selector was not located; do not label this a full Envs-FORGE reproduction.

## Source entry points

- [sdgsystem/agentic_data/terminal_bench.py](https://github.com/DataArcTech/DataArc-SynData-Toolkit/blob/2a1d65ec8dcfaea2458d67e1fb18078cce6420b9/sdgsystem/agentic_data/terminal_bench.py)
- [examples/syn_agentic_data/run_terminal_bench.py](https://github.com/DataArcTech/DataArc-SynData-Toolkit/blob/2a1d65ec8dcfaea2458d67e1fb18078cce6420b9/examples/syn_agentic_data/run_terminal_bench.py)
- [configs/syn_agentic_terminal_bench.yaml](https://github.com/DataArcTech/DataArc-SynData-Toolkit/blob/2a1d65ec8dcfaea2458d67e1fb18078cce6420b9/configs/syn_agentic_terminal_bench.yaml)

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
