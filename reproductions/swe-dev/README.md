# SWE-Dev (THUDM)

Status: **planned; no reproduction has run**. Supporting component; target **100 input cases**, not 100 newly synthesized tasks.

[Upstream source](https://github.com/THUDM/SWE-Dev/tree/72917b610c3749c9e1236b29effd629f8aca6a99) · MIT · pinned commit `72917b610c3749c9e1236b29effd629f8aca6a99`.

## Scope

Reproduce test construction on 100 existing task inputs after a repository baseline exists.

**Native input:** Repository task descriptions, patches and execution environments.

**Native output:** Generated behavior descriptions and executable tests.

**Harbor boundary:** Attach its generated verifier to the source task in a separately labeled export copy.

**Known limitation:** This is verifier generation, not a new learner-writes-tests pipeline.

## Source entry points

- [swedev/testcases/get_descriptions.py](https://github.com/THUDM/SWE-Dev/blob/72917b610c3749c9e1236b29effd629f8aca6a99/swedev/testcases/get_descriptions.py)
- [swedev/testcases/get_testcases.py](https://github.com/THUDM/SWE-Dev/blob/72917b610c3749c9e1236b29effd629f8aca6a99/swedev/testcases/get_testcases.py)
- [swedev/testcases/eval_testcases.py](https://github.com/THUDM/SWE-Dev/blob/72917b610c3749c9e1236b29effd629f8aca6a99/swedev/testcases/eval_testcases.py)

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
