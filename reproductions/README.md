# Upstream reproduction experiments

Reproduce existing public implementations one by one, using each project's native inputs and emitting a common **Harbor task format**. Target **100 task exports per generation experiment**, after smaller runs establish that the original workflow and export behave correctly.

Branch: `codex/upstream-reproductions`, created from local `main` at `1a8bad5774e062d0d73a7d79f9d6a95540801c83`. The previous harness work remains on `codex/dynamic-harbor-curation`.

The first five implementations have completed a remote generation/export pilot.
See [RUNBOOK.md](RUNBOOK.md) for commands, [ARTIFACTS.md](ARTIFACTS.md) for the
actual Harbor tasks and traces, and [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)
for the original stage contracts. The fresh **$500 reproduction budget** is
separate from earlier Tasksmith campaigns.
The completed pilot accounts for **$15.46**, with **$484.54 remaining** and the
worker terminated. See [COSTS.md](COSTS.md) for the SDK reports, rate estimates and
cloud allowance; these are not invoiced charges.

| Pipeline | Generated Harbor exports | Fresh no-op/reference passes | Result |
| --- | --- | --- | --- |
| [SETA Seed2Synth](seta-seed2synth/RESULTS.md) | 3 | 2 original; 3 with a reference-precision repair | Offline blind Sonnet success on one task |
| [SETA Evol](seta-evol/RESULTS.md) | 2 | 2 | Both original CHANGE_CONTEXT children pass |
| [Endless Terminals](endless-terminals/RESULTS.md) | 5 from 11 attempts | 1 | All five have documented quality failures, including three demonstrated reward shortcuts |
| [TMax](tmax/RESULTS.md) | 1 | 1 after adding a missing verifier dependency | Native 51-test success and Harbor replay success |
| [SWE-smith](swe-smith/RESULTS.md) | 5 | 5 | Native procedural mutation validation; offline blind Sonnet success on one |

**16 distinct task exports; 12 pass execution contrast when the two explicit
repairs are included.** Variants and adversarial candidates do not inflate that
count. No task is labeled training-approved by this pilot. We reproduced the
released generation paths and preserved quality failures instead of changing
their verifiers. The 100-per-pipeline scale-up remains a later milestone.

## Recommended generation order

| Order | Reproduction | Reason for this position |
| --- | --- | --- |
| 1 | [SETA Seed2Synth](seta-seed2synth/README.md) | Native Harbor output and a standalone synthesis mode; first prove one complete upstream run. |
| 2 | [SETA Evol](seta-evol/README.md) | Reuse the SETA installation and runtime; change only to its published evolution path. |
| 3 | [Endless Terminals](endless-terminals/README.md) | Establish the simpler Apptainer generation path and its existing Harbor conversion before TMax. |
| 4 | [TMax](tmax/README.md) | Reuse the Apptainer runtime established for Endless, then exercise the richer sampler and fixtures. |
| 5 | [SWE-smith](swe-smith/README.md) | First repository synthesis reproduction; prepared profiles and an existing Modal execution path reduce setup uncertainty. |
| 6 | [CLI-Gym](cli-gym/README.md) | Reuse healthy repository images from the SWE-smith stage and reproduce environment inversion. |
| 7 | [SWE-Flow](swe-flow/README.md) | Next reproduce repository reconstruction after the prepared-image workflow is stable. |
| 8 | [TerminalWorld (EuniAI)](terminalworld/README.md) | Native Harbor output, but source-state reconstruction is less predictable; tackle it after container execution is established. |
| 9 | [SWE-gen](swe-gen/README.md) | Start PR mining once the reproduction runtime is proven, so repository bootstrap failures are easier to isolate. |
| 10 | [SWE-Next](swe-next/README.md) | Reproduce dependency-profile reuse and commit mining after a direct PR-to-Harbor baseline. |
| 11 | [R2E-Gym / SWEGEN](r2e-gym/README.md) | A separate commit-generation baseline with more repository-specific configuration. |
| 12 | [R2E](r2e/README.md) | Reproduce the narrower function-level task unit after repository-level workflows. |
| 13 | [SEC-bench](sec-bench/README.md) | Specialized native build and reproduction requirements make this a later repository workflow. |
| 14 | [DataArc terminal synthesis (Envs-FORGE-linked code)](dataarc-terminal/README.md) | Reproduce the available branch after core synthesis/evolution baselines, with explicit partial-release scope. |
| 15 | [SCALER](scaler/README.md) | A different task unit and domain; reproduce separately after terminal and repository baselines. |

For this objective, SETA comes before the earlier TMax/Endless comparison: its existing Seed2Synth and Evol outputs are already Harbor tasks, so the first milestone needs less format adaptation. Endless then establishes the Apptainer path used by TMax. SWE-smith precedes CLI-Gym because the latter starts from healthy SWE-smith images.

The order is an engineering judgment from the inspected source, not a measured runtime or cost ranking. Input/model availability may change a position; record the reason if it does. Use supported upstream examples before attempting arbitrary repositories or new source domains.

## How we run each reproduction

```text
Pinned upstream code + native input
  -> original generator, agents, prompts and acceptance rules
  -> preserved native outputs
  -> existing Harbor converter, or minimal packaging adapter
  -> native-versus-Harbor execution comparison
  -> independent validation report
  -> 100 exported tasks and a reproducible result summary
```

Ramp: **1 -> 5 -> 20 -> 100**. First establish working smoke runs across the first five in order, then scale using measured cost and yield. A smoke result is not a paper reproduction or a 100-task result.

For every stage, preserve the native acceptance count, export count and independent audit count separately. Native failures and quality weaknesses are results to report; they are not permission to redesign the method. See [PROTOCOL.md](PROTOCOL.md).

## Supporting components

These projects do not all generate complete new tasks. Reproduce their actual function on a panel of 100 suitable inputs and attach their results to Harbor task exports where applicable. Their inputs, traces or transformations must not inflate the new-task total.

- [RepoLaunch](repolaunch/README.md): Run when setup is needed for repository reproductions; benchmark 100 setup cases separately.
- [SWE-Flow-Trace](swe-flow-trace/README.md): A dependency of the SWE-Flow reproduction, with its own source pin.
- [SWE-bench-Live](swe-bench-live/README.md): Use the published crawler as a separate source-collection reproduction for PR workflows.
- [SWE-rebench V2 released components](swe-rebench-v2/README.md): Reproduce published build/evaluation components; the complete production miner was not located.
- [SWE-Dev (THUDM)](swe-dev/README.md): Reproduce test construction on 100 existing task inputs after a repository baseline exists.
- [SWE-Mutation](swe-mutation/README.md): Reproduce on 100 existing tasks to measure verifier weaknesses separately from generation.
- [harden-v0](harden-v0/README.md): Reproduce hardening on 100 generated tasks after their unmodified baselines have been frozen.
- [SecVerifier](secverifier/README.md): Run with the SEC-bench reproduction on its supported inputs.

## Folder convention

Each reproduction has its own folder, source pin and status record. SETA synthesis/evolution are separate experiments over the same upstream repository; SWE-Flow's tracer has a separate source pin.

```text
reproductions/
  README.md
  PROTOCOL.md
  index.json
  seta-seed2synth/
    README.md
    experiment.json
    config/          # exact native config, once prepared
    patches/         # compatibility changes, if necessary
    runs/            # ignored native artifacts and evidence
    harbor/          # ignored export copies
    RESULTS.md       # measured outcomes, after execution
  seta-evol/
  endless-terminals/
  tmax/
  ...
```

Only README and experiment.json exist inside each experiment at scaffold time. Configurations, launchers and results are added when prepared and verified. Do not invent successful commands or model settings in advance. `index.json` records the sequence; each `experiment.json` records the pinned source, native input/output and progress.

All target execution, image construction and task-fixture generation happens on **Modal or Daytona**. Prefer a remote VM/DinD worker capable of running the project's original tools when they assume a local Docker daemon or Apptainer. Reuse that worker recipe once verified; do not turn this prerequisite into a new general orchestration framework. Runtime environments remain isolated per upstream dependency stack.

Native output, converted output and audit evidence stay separate. Generated artifacts are gitignored and may be uploaded to the chosen Hugging Face artifact destination with immutable manifests. Upstream default upload destinations must be redirected or disabled so reproductions do not write into another project's dataset.

## Methods not in the runnable queue

TaskPilot, Environment Evolution, CalibForge, Recursive Task Synthesis, CLI-Universe, SkillSynth, Meta-Task, SSR and Socratic-SWE had no complete official generator located in the source audit. Under this experiment's **existing-implementation-only** scope, they are deferred rather than reimplemented. Revisit when source is available.

SWE-rebench V2 and the Envs-FORGE-linked DataArc branch are explicitly labeled partial releases. Importing released tasks or running available components does not reproduce an absent production generator. SCALER keeps its original problem-domain semantics rather than being presented as a repository coding pipeline.

## Next milestone

Use the saved artifacts to reproduce the observed checks without generation cost,
then expand the small panels toward five, 20 and 100 inputs per generator. Keep
native workflow fidelity and independent quality as separate measurements.
Endless's observed verifier failures should inform any later hardening experiment;
they must not be hidden by rewriting this baseline. SWE-smith needs additional
supported repositories before making a diversity claim, and SETA needs further
seed and evolution strategies before making a curriculum claim.
