# harden-v0

Status: **planned; no reproduction has run**. Supporting component; target **100 input cases**, not 100 newly synthesized tasks.

[Upstream source](https://github.com/few-sh/harden-v0/tree/342b8474e0c0cf96e4a8313fd2e26c7a11d51193) · Apache-2.0 · pinned commit `342b8474e0c0cf96e4a8313fd2e26c7a11d51193`.

## Scope

Reproduce hardening on 100 generated tasks after their unmodified baselines have been frozen.

**Native input:** Existing Harbor tasks.

**Native output:** Hardened task revisions and hacker/fixer/solver evidence.

**Harbor boundary:** Already Harbor-shaped; keep before/after copies and publish separate hardening results.

**Known limitation:** These are transformations of existing tasks, not 100 new source tasks.

## Source entry points

- [harden/loop.py](https://github.com/few-sh/harden-v0/blob/342b8474e0c0cf96e4a8313fd2e26c7a11d51193/harden/loop.py)
- [harden/agent.py](https://github.com/few-sh/harden-v0/blob/342b8474e0c0cf96e4a8313fd2e26c7a11d51193/harden/agent.py)
- [harden/workspace.py](https://github.com/few-sh/harden-v0/blob/342b8474e0c0cf96e4a8313fd2e26c7a11d51193/harden/workspace.py)

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
