# SWE-gen

Status: **planned; no reproduction has run**. Position 9 in the generation queue; target **100 Harbor task exports**.

[Upstream source](https://github.com/abundant-ai/SWE-gen/tree/14e185f413f7bff03f8f9fec6fb246681bf61d74) · Apache-2.0 · pinned commit `14e185f413f7bff03f8f9fec6fb246681bf61d74`.

## Scope

Start PR mining once the reproduction runtime is proven, so repository bootstrap failures are easier to isolate.

**Native input:** PRs and their repository, issue, patch and test evidence.

**Native output:** Harbor task directories.

**Harbor boundary:** Retain its native task artifacts and NOP/oracle validation route.

**Known limitation:** Keep its original coding-agent implementation and PR filters. Additional coverage review is a separate audit.

## Source entry points

- [src/swegen/create/task_instruction.py](https://github.com/abundant-ai/SWE-gen/blob/14e185f413f7bff03f8f9fec6fb246681bf61d74/src/swegen/create/task_instruction.py)
- [src/swegen/create/claude_code_runner.py](https://github.com/abundant-ai/SWE-gen/blob/14e185f413f7bff03f8f9fec6fb246681bf61d74/src/swegen/create/claude_code_runner.py)
- [src/swegen/tools/validate.py](https://github.com/abundant-ai/SWE-gen/blob/14e185f413f7bff03f8f9fec6fb246681bf61d74/src/swegen/tools/validate.py)

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
