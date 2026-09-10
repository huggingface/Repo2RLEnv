Current execution results: [RESULTS.md](RESULTS.md). Runnable stages: `python reproductions/run.py plan r2e-gym`.

# R2E-Gym / SWEGEN

Status: **planned; no reproduction has run**. Position 11 in the generation queue; target **100 Harbor task exports**.

[Upstream source](https://github.com/R2E-Gym/R2E-Gym/tree/0d94c4eb9431cd195c55a7ea3abd54006c9a1735) · Apache-2.0 · pinned commit `0d94c4eb9431cd195c55a7ea3abd54006c9a1735`.

## Scope

A separate commit-generation baseline with more repository-specific configuration.

**Native input:** A supported repository with its upstream installation and test configuration.

**Native output:** Commit-derived executable coding tasks and reference patches.

**Harbor boundary:** Translate existing task records into Harbor without inventing new instructions or grading rules.

**Known limitation:** Use supported repositories initially and keep source-specific setup changes explicit.

## Source entry points

- [docs/ENV_GENERATION.md](https://github.com/R2E-Gym/R2E-Gym/blob/0d94c4eb9431cd195c55a7ea3abd54006c9a1735/docs/ENV_GENERATION.md)
- [src/r2egym/repo_analysis/](https://github.com/R2E-Gym/R2E-Gym/tree/0d94c4eb9431cd195c55a7ea3abd54006c9a1735/src/r2egym/repo_analysis)

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
