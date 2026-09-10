Current execution results: [RESULTS.md](RESULTS.md). Runnable stages: `python reproductions/run.py plan sec-bench`.

# SEC-bench

Status: **planned; no reproduction has run**. Position 13 in the generation queue; target **100 Harbor task exports**.

[Upstream source](https://github.com/SEC-bench/SEC-bench/tree/31eb43485a3de47da260be0f978528b1f2314415) · MIT · pinned commit `31eb43485a3de47da260be0f978528b1f2314415`.

## Scope

Specialized native build and reproduction requirements make this a later repository workflow.

**Native input:** Supported documented defects, affected projects and fixed revisions.

**Native output:** Built security benchmark instances and evaluation artifacts.

**Harbor boundary:** Wrap existing isolated instances and their original reproduction/regression checks in Harbor.

**Known limitation:** Preserve the intended learner task and scoring; report upstream instance-construction limitations.

## Source entry points

- [secb/preprocessor/](https://github.com/SEC-bench/SEC-bench/tree/31eb43485a3de47da260be0f978528b1f2314415/secb/preprocessor)
- [secb/evaluator/build_eval_instances.py](https://github.com/SEC-bench/SEC-bench/blob/31eb43485a3de47da260be0f978528b1f2314415/secb/evaluator/build_eval_instances.py)
- [secb/evaluator/eval_instances.py](https://github.com/SEC-bench/SEC-bench/blob/31eb43485a3de47da260be0f978528b1f2314415/secb/evaluator/eval_instances.py)

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
