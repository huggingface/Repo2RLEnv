# Endless Terminals

Status: **planned; no reproduction has run**. Position 3 in the generation queue; target **100 Harbor task exports**.

[Upstream source](https://github.com/kanishkg/endless-terminals/tree/99f4c74b75faacf21e53d3dc01df170902e924cb) · Apache-2.0 · pinned commit `99f4c74b75faacf21e53d3dc01df170902e924cb`.

## Scope

Establish the simpler Apptainer generation path and its existing Harbor conversion before TMax.

**Native input:** Its original task-generation settings, prompts and configured model.

**Native output:** Task JSON, initial/completion tests, Apptainer definitions and sampled solutions.

**Harbor boundary:** Use the upstream converter first; compare native and converted verifier outcomes on identical initial and solution states.

**Known limitation:** The conversion path has its own model/sampling requirements. Initial-state build success is distinct from completion-verifier validity.

## Source entry points

- [generate_tasks.py](https://github.com/kanishkg/endless-terminals/blob/99f4c74b75faacf21e53d3dc01df170902e924cb/generate_tasks.py)
- [generator/apptainer_def_gen.py](https://github.com/kanishkg/endless-terminals/blob/99f4c74b75faacf21e53d3dc01df170902e924cb/generator/apptainer_def_gen.py)
- [generator/convert_to_harbor/convert_sif_docker.py](https://github.com/kanishkg/endless-terminals/blob/99f4c74b75faacf21e53d3dc01df170902e924cb/generator/convert_to_harbor/convert_sif_docker.py)
- [generator/convert_to_harbor/add_reward_file.py](https://github.com/kanishkg/endless-terminals/blob/99f4c74b75faacf21e53d3dc01df170902e924cb/generator/convert_to_harbor/add_reward_file.py)

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
