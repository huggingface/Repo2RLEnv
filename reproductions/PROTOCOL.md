# Reproduction and Harbor export protocol

This protocol applies to the experiments in this directory. It reproduces existing generation workflows; Tasksmith integration and methodological improvements come later.

## 1. Freeze the reproduction identity

Record the upstream URL and full commit, license, installation lockfiles, runtime image, native entry command, input manifest and all random seeds. Record generator, judge, solver and converter model identities separately, plus the original agent implementation and sampling settings.

Use an upstream-supported input subset first. Freeze the ordered candidate list before generation and retain candidate IDs for failed attempts. A target of 100 task outputs may require more than 100 inputs or attempts; report both.

Distinguish three claims:

- **Workflow reproduction:** the released code and algorithm execute on the chosen inputs.
- **Configuration reproduction:** the published models, prompts, settings and runtime assumptions are matched, with any differences disclosed.
- **Paper-result reproduction:** the original experiment and metrics are reproduced. A small sample of tasks does not support this stronger claim.

A model substitution, changed prompt, altered acceptance policy or new input domain is a named deviation. Model availability does not justify claiming exact equivalence. Do not introduce Pi/OpenCode or LangGraph into a project that uses a different original agent or controller.

## 2. Make the native implementation run remotely

Build target images and fixtures only on Modal or Daytona. Run the upstream implementation in its own environment with its original dependency stack. For tools that expect Docker or Apptainer, use an appropriate remote worker and verify its required capabilities before synthesis.

Allowed adaptation is narrow: dependency/API compatibility, remote execution configuration, resource limits, output destinations, and Harbor packaging. Save every upstream code patch, reason, before/after behavior and test evidence. Preserve original files at the pinned revision.

Do not rewrite Apptainer generation into Docker-native generation just for convenience. Use the original toolchain and its published converter where available. If a native capability cannot run on the chosen provider, report and resolve that runtime issue explicitly.

Original upstream acceptance remains the baseline. A defective verifier or weak author self-review is an observation. Methodological repairs become a separately named experiment after the baseline has been preserved.

## 3. Keep three products separate

- **Native:** exact upstream-generated task files and its own verdict/metadata.
- **Harbor:** a separate export of that task, using an existing converter first.
- **Audit:** our independently collected evidence, including defects and uncertainty.

Prefer a converter already shipped by the project. Where none exists, implement only the required task-layout, workspace, solution invocation and reward-entry-point mapping. Preserve instructions, test semantics, allowed actions and scoring. Record compiler/converter code and model versions.

If an upstream artifact has no oracle, record that fact. Do not synthesize a new reference solution and call it part of an unchanged upstream reproduction. If an original successful trajectory can be replayed, label it as a derived witness, retain provenance, and test it separately.

An existing released dataset may supply smoke fixtures or seeds, but downloading 100 existing tasks is a replay/import experiment, not generating 100 new tasks.

## 4. Validate the conversion and report task quality

For each task, validate the Harbor schema against the chosen pinned runtime. Confirm assets are present and initial-state checks actually execute. Run the native verifier and the Harbor-wrapped verifier on equivalent states.

When available and appropriate, use:

1. Untouched/no-op state.
2. The upstream reference solution or a recorded upstream successful solution.
3. A documented partial or failing solution for a small audit sample.

Compare test collection, test identities, execution results and rewards. An infrastructure error or zero-test run is not evidence of a successful behavioral contrast. A positive reference must work on a fresh instance, not only on the author's modified workspace.

Do not impose a requirement that every no-op test fail. Existing regression tests may pass; expected baseline behavior depends on the original task. Record when the upstream grader does not distinguish the relevant states.

Review representative instructions and solver traces for missing context, leaked answers and shortcuts. Freeze the original output even if the independent review finds a weakness. Report whether the task meets our quality bar separately from whether the upstream implementation reproduced successfully. Any extra isolation policy that changes available actions must be labeled and compared, not silently merged into the baseline.

Use a small blind solver sample to check usability, with exact settings recorded. That optional common audit is distinct from reproducing the paper's learner/training experiments. Do not start model training merely to generate task artifacts.

## 5. Scale in stages

| Stage | Purpose | Required evidence |
| --- | --- | --- |
| 1 output | Prove installation, native entry command and artifact production | Source/runtime pins, full log, actual native files |
| 5 outputs | Prove the path is repeatable and exports preserve behavior | Native/Harbor comparison, task review and all failures retained |
| 20-input pilot | Measure throughput, yield, cost and dominant failures | Per-stage counts, inference/cloud charges, feasible path to 100 |
| 100 outputs | Complete the generation reproduction sample | Unique task manifests, native verdicts, export parity evidence, audit findings and rerun recipe |

For components, use input cases in place of task outputs. Run the next generation experiment after the current one reaches its measured milestone or an explicitly documented external blocker; do not quietly replace a difficult experiment.

Honor existing spending constraints. Estimate remaining cost from the pilot and include failed jobs, retries, idle resources, solver calls and conversion calls. Do not infer a fresh budget from the 100-task target.

## 6. What each result summary must contain

Write RESULTS.md after execution, with:

- Exact source commit, input manifest, models, native commands and runtime.
- Fidelity classification and every deviation from the upstream configuration.
- Candidate, attempted, native-emitted, native-accepted, Harbor-exported, parity-passed and independent-audit-passed counts.
- Missing oracle or incomplete evidence counts; unknown is not pass.
- Reproduction of the original output semantics and a record of any conversion mismatch.
- Setup, generation, conversion, audit and rollout costs/time separately.
- Representative task examples, failure categories and native-versus-Harbor outcomes.
- Durable artifact URIs and hashes, source/parent lineage, and a rerun procedure.

Task directories are counted uniquely. Evolved or hardened children retain parents. Component outputs, repeated runs, failed copies and replayed dataset rows do not inflate new-task counts.

Keep credentials out of committed configs and logs. Store raw run data under ignored run directories; commit only summaries, patches and artifact references. Upload to the chosen project-owned destination only.
