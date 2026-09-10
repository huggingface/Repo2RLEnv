# CLI-Gym

Status: **planned; no reproduction has run**. Position 6 in the generation queue; target **100 Harbor task exports**.

[Upstream source](https://github.com/LiberCoders/CLI-Gym/tree/48bb920b728a25a55a5b442303e901919654599e) · MIT · pinned commit `48bb920b728a25a55a5b442303e901919654599e`.

## Scope

Reuse healthy repository images from the SWE-smith stage and reproduce environment inversion.

**Native input:** Healthy SWE-smith repository images and upstream destruction settings.

**Native output:** Older Terminal-Bench task layout, Docker/Compose configuration and selected test runners.

**Harbor boundary:** Translate the old task layout and reward entry point while preserving the original grading behavior.

**Known limitation:** The assembly path lacks our desired restoration oracle and may retain broken-runner cases. Audit these explicitly instead of changing upstream acceptance.

## Source entry points

- [src/cli_gym/cli.py](https://github.com/LiberCoders/CLI-Gym/blob/48bb920b728a25a55a5b442303e901919654599e/src/cli_gym/cli.py)
- [src/cli_gym/build_destruction_task/](https://github.com/LiberCoders/CLI-Gym/tree/48bb920b728a25a55a5b442303e901919654599e/src/cli_gym/build_destruction_task)
- [src/cli_gym/assemble_problem_instance/](https://github.com/LiberCoders/CLI-Gym/tree/48bb920b728a25a55a5b442303e901919654599e/src/cli_gym/assemble_problem_instance)
- [src/cli_gym/utils/docker_utils.py](https://github.com/LiberCoders/CLI-Gym/blob/48bb920b728a25a55a5b442303e901919654599e/src/cli_gym/utils/docker_utils.py)

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
