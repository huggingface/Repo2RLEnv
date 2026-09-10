# RepoLaunch

Status: **planned; no reproduction has run**. Supporting component; target **100 input cases**, not 100 newly synthesized tasks.

[Upstream source](https://github.com/microsoft/RepoLaunch/tree/db3eb23a6b6b1260ab72d7253adb77df7c3cf5fe) · MIT · pinned commit `db3eb23a6b6b1260ab72d7253adb77df7c3cf5fe`.

## Scope

Run when setup is needed for repository reproductions; benchmark 100 setup cases separately.

**Native input:** Repository revisions requiring setup.

**Native output:** Working environment recipes, commands, parsers and reuse evidence.

**Harbor boundary:** Use build records as dependencies of Harbor exports, not as 100 newly generated learner tasks.

**Known limitation:** Bootstrap success and task validity have different denominators.

## Source entry points

- [launch/core/workflow.py](https://github.com/microsoft/RepoLaunch/blob/db3eb23a6b6b1260ab72d7253adb77df7c3cf5fe/launch/core/workflow.py)
- [launch/agent/setup/](https://github.com/microsoft/RepoLaunch/tree/db3eb23a6b6b1260ab72d7253adb77df7c3cf5fe/launch/agent/setup)
- [launch/agent/organize/](https://github.com/microsoft/RepoLaunch/tree/db3eb23a6b6b1260ab72d7253adb77df7c3cf5fe/launch/agent/organize)
- [launch/scripts/adjacent_commit_run.py](https://github.com/microsoft/RepoLaunch/blob/db3eb23a6b6b1260ab72d7253adb77df7c3cf5fe/launch/scripts/adjacent_commit_run.py)

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
