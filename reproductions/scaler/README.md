Current execution results: [RESULTS.md](RESULTS.md). Runnable stages: `python reproductions/run.py plan scaler`.

# SCALER

Status: **planned; no reproduction has run**. Position 15 in the generation queue; target **100 Harbor task exports**.

[Upstream source](https://github.com/ALEX-nlp/SCALER/tree/60c6c5037866c718f4c001ea338f9c5a91cb01ae) · Apache-2.0 · pinned commit `60c6c5037866c718f4c001ea338f9c5a91cb01ae`.

## Scope

A different task unit and domain; reproduce separately after terminal and repository baselines.

**Native input:** Upstream seed problems and parameterized environment-generation configuration.

**Native output:** Executable problem generators and sampled reasoning/problem instances.

**Harbor boundary:** Represent original problem input, response interface and verifier in Harbor without inventing repository-editing semantics.

**Known limitation:** This is a specialist problem-family reproduction, not automatically a coding-agent environment dataset.

## Source entry points

- [SCALER/api_generate_generator_for_environment.py](https://github.com/ALEX-nlp/SCALER/blob/60c6c5037866c718f4c001ea338f9c5a91cb01ae/SCALER/api_generate_generator_for_environment.py)
- [SCALER/generate_problem_from_environment.py](https://github.com/ALEX-nlp/SCALER/blob/60c6c5037866c718f4c001ea338f9c5a91cb01ae/SCALER/generate_problem_from_environment.py)
- [recipe/environment/](https://github.com/ALEX-nlp/SCALER/tree/60c6c5037866c718f4c001ea338f9c5a91cb01ae/recipe/environment)

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
