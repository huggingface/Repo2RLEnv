# TMax

Status: **planned; no reproduction has run**. Position 4 in the generation queue; target **100 Harbor task exports**.

[Upstream source](https://github.com/hamishivi/tmax/tree/7387d2f9142397a458dc39f0827a2ab0b4c03cda) · Apache-2.0 · pinned commit `7387d2f9142397a458dc39f0827a2ab0b4c03cda`.

## Scope

Reuse the Apptainer runtime established for Endless, then exercise the richer sampler and fixtures.

**Native input:** Upstream domain/skill/fixture sampling configuration and base-image recipes.

**Native output:** Apptainer task bundles, fixtures and optional sampled solution summaries.

**Harbor boundary:** Use its existing Harbor converter; preserve the generated tests and task semantics.

**Known limitation:** The upstream method skips teacher correctness validation and its converter omits solve.sh. Record missing reference evidence; do not silently add a new oracle-generation stage.

## Source entry points

- [rl_data/generate_tasks.py](https://github.com/hamishivi/tmax/blob/7387d2f9142397a458dc39f0827a2ab0b4c03cda/rl_data/generate_tasks.py)
- [rl_data/generator/](https://github.com/hamishivi/tmax/tree/7387d2f9142397a458dc39f0827a2ab0b4c03cda/rl_data/generator)
- [rl_data/containers/](https://github.com/hamishivi/tmax/tree/7387d2f9142397a458dc39f0827a2ab0b4c03cda/rl_data/containers)
- [rl_data/scripts/analyze/convert_to_harbor.py](https://github.com/hamishivi/tmax/blob/7387d2f9142397a458dc39f0827a2ab0b4c03cda/rl_data/scripts/analyze/convert_to_harbor.py)

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
