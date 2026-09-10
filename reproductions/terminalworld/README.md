# TerminalWorld (EuniAI)

Status: **planned; no reproduction has run**. Position 8 in the generation queue; target **100 Harbor task exports**.

[Upstream source](https://github.com/EuniAI/TerminalWorld/tree/784698ba93735470ce1664bff2ec44bcd7b28e15) · Apache-2.0 · pinned commit `784698ba93735470ce1664bff2ec44bcd7b28e15`.

## Scope

Native Harbor output, but source-state reconstruction is less predictable; tackle it after container execution is established.

**Native input:** Asciinema recordings and associated metadata accepted by the upstream filters.

**Native output:** Harbor tasks reconstructed from recordings.

**Harbor boundary:** Keep native Harbor tasks; make runtime configuration explicit and preserve reconstruction evidence.

**Known limitation:** Use the recording-derived project. Record missing state and weak status-file checks as findings, without redesigning the verifier during baseline reproduction.

## Source entry points

- [data_retrieval/](https://github.com/EuniAI/TerminalWorld/tree/784698ba93735470ce1664bff2ec44bcd7b28e15/data_retrieval)
- [data_filtering/](https://github.com/EuniAI/TerminalWorld/tree/784698ba93735470ce1664bff2ec44bcd7b28e15/data_filtering)
- [task_synthesis/](https://github.com/EuniAI/TerminalWorld/tree/784698ba93735470ce1664bff2ec44bcd7b28e15/task_synthesis)
- [environment_building/](https://github.com/EuniAI/TerminalWorld/tree/784698ba93735470ce1664bff2ec44bcd7b28e15/environment_building)
- [test_generation/](https://github.com/EuniAI/TerminalWorld/tree/784698ba93735470ce1664bff2ec44bcd7b28e15/test_generation)

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
