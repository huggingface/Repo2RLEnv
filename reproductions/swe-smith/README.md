# SWE-smith

Status: **planned; no reproduction has run**. Position 5 in the generation queue; target **100 Harbor task exports**.

[Upstream source](https://github.com/SWE-bench/SWE-smith/tree/9b74ac08118a85c39c356802f7961893af73e07f) · MIT · pinned commit `9b74ac08118a85c39c356802f7961893af73e07f`.

## Scope

First repository synthesis reproduction; prepared profiles and an existing Modal execution path reduce setup uncertainty.

**Native input:** A supported repository profile and healthy upstream image.

**Native output:** Mutation patches, issue descriptions and validated SWE-bench-style instances.

**Harbor boundary:** Package the exact defective snapshot, existing reference restoration and upstream test command into Harbor.

**Known limitation:** Use a supported repository first. Do not introduce a new mutation strategy or claim arbitrary repository support.

## Source entry points

- [scripts/bug_gen_modal.py](https://github.com/SWE-bench/SWE-smith/blob/9b74ac08118a85c39c356802f7961893af73e07f/scripts/bug_gen_modal.py)
- [swesmith/bug_gen/](https://github.com/SWE-bench/SWE-smith/tree/9b74ac08118a85c39c356802f7961893af73e07f/swesmith/bug_gen)
- [swesmith/issue_gen/](https://github.com/SWE-bench/SWE-smith/tree/9b74ac08118a85c39c356802f7961893af73e07f/swesmith/issue_gen)
- [swesmith/harness/valid.py](https://github.com/SWE-bench/SWE-smith/blob/9b74ac08118a85c39c356802f7961893af73e07f/swesmith/harness/valid.py)

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
