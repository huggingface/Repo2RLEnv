Current execution results: [RESULTS.md](RESULTS.md). Runnable stages: `python reproductions/run.py plan swe-flow`.

# SWE-Flow

Status: **planned; no reproduction has run**. Position 7 in the generation queue; target **100 Harbor task exports**.

[Upstream source](https://github.com/Hambaobao/SWE-Flow/tree/7da5b046fa1dc184674e4e94a9989be56c39e4e7) · MIT · pinned commit `7da5b046fa1dc184674e4e94a9989be56c39e4e7`.

## Scope

Next reproduce repository reconstruction after the prepared-image workflow is stable.

**Native input:** A working supported Python repository and traces from SWE-Flow-Trace.

**Native output:** Partial codebases, restoration patches, schedules and specifications.

**Harbor boundary:** Package the upstream skeleton, specification, restoration patch and selected tests; execute labels rather than assuming they passed.

**Known limitation:** Trace-derived test IDs are not execution receipts. Preserve original archives privately and keep them outside the learner image.

## Source entry points

- [docs/pipeline.md](https://github.com/Hambaobao/SWE-Flow/blob/7da5b046fa1dc184674e4e94a9989be56c39e4e7/docs/pipeline.md)
- [sweflow/extensions/python/rdg.py](https://github.com/Hambaobao/SWE-Flow/blob/7da5b046fa1dc184674e4e94a9989be56c39e4e7/sweflow/extensions/python/rdg.py)
- [sweflow/extensions/python/schedule.py](https://github.com/Hambaobao/SWE-Flow/blob/7da5b046fa1dc184674e4e94a9989be56c39e4e7/sweflow/extensions/python/schedule.py)
- [sweflow/extensions/python/create_codebase.py](https://github.com/Hambaobao/SWE-Flow/blob/7da5b046fa1dc184674e4e94a9989be56c39e4e7/sweflow/extensions/python/create_codebase.py)
- [sweflow/extensions/python/create_specification.py](https://github.com/Hambaobao/SWE-Flow/blob/7da5b046fa1dc184674e4e94a9989be56c39e4e7/sweflow/extensions/python/create_specification.py)

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
