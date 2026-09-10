# SWE-bench-Live

Status: **planned; no reproduction has run**. Supporting component; target **100 input cases**, not 100 newly synthesized tasks.

[Upstream source](https://github.com/microsoft/SWE-bench-Live/tree/9a273490f230bc0e54de75e8f865a0d90f7eb1f6) · MIT · pinned commit `9a273490f230bc0e54de75e8f865a0d90f7eb1f6`.

## Scope

Use the published crawler as a separate source-collection reproduction for PR workflows.

**Native input:** Repository and issue/PR crawl configuration.

**Native output:** Collected issue/PR task evidence and curated records.

**Harbor boundary:** Fully executable curated records can be imported; collection-only records do not count as generated environments.

**Known limitation:** Do not count crawl records as built tasks or substitute existing dataset downloads for a generation run.

## Source entry points

- [curation/crawl_repo.py](https://github.com/microsoft/SWE-bench-Live/blob/9a273490f230bc0e54de75e8f865a0d90f7eb1f6/curation/crawl_repo.py)
- [curation/swe_task_crawling/](https://github.com/microsoft/SWE-bench-Live/tree/9a273490f230bc0e54de75e8f865a0d90f7eb1f6/curation/swe_task_crawling)

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
