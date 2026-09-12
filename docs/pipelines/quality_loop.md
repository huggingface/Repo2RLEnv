# Review and repair a Harbor task

`repo2rlenv quality run` reviews an existing Harbor task, optionally reuses its
rollout, and can run missing validation and repair the task remotely. It is a
component shared by generation recipes, not another generator. Original tasks are
preserved; each repair produces a separate Harbor directory.

This implements the practical review approach from the [quality pilots](quality_pilot.md).
It does **not** replace the older, stricter `QualityReport.accepted` contract.

## Execution and decisions

```mermaid
flowchart TD
    A[Harbor task directory] --> B[Snapshot and verify content identity]
    E[Optional baseline / oracle / solver results] --> C[Bind evidence to this exact task]
    B --> C
    C --> D{Remote validation enabled?}
    D -->|Yes| F[Reuse controls or run fresh nop and oracle]
    D -->|No| U[Static review with bounded reads and optional escalation]
    U --> V[Report without new trials or edits]
    F --> G[Review task, tests, reference and available trace]
    G --> H{Need more evidence?}
    H -->|Read request| I[Bounded file excerpts]
    I --> G
    H -->|Unresolved| J[Optional stronger reviewer]
    J -->|Resolved| K
    J -->|Still unresolved| S
    H -->|Enough| K[Propose wrong solution and valid alternative]
    K --> L[Run private reference-then-mutation probes remotely]
    L --> M{Task looks sound and controls agree?}
    M -->|Yes| N[Reuse current rollout or run blind Sonnet / OpenAI]
    N --> O[Review submitted changes, trace and verifier outcomes]
    M -->|Defect| P[Ground the failure in concrete evidence]
    O -->|Defect| P
    P --> Q{Repair enabled and budget remains?}
    Q -->|Yes| R[Targeted task edit or diagnosed alternative-probe correction]
    R --> F
    Q -->|No| S[Report needs repair or evidence]
    O -->|Sound| T[Select under practical-generation-v1]
```

The diagram's execution steps require `--run-rollout` or `--repair`; a review-only
run makes model calls but creates no worker. An active loop reuses one worker and
its runtime and Docker build cache throughout the loop. Modal and Daytona use the
same `RemoteWorker` contract. Target Docker builds, tests, probe scripts and solver
commands run remotely; the controller only reads, hashes and edits artifacts.

## Use it

Install the provider and Harbor extras and build the owned runtime from this checkout:

```bash
uv sync --extra modal --extra harbor
uv build --wheel
```

Use an existing campaign ledger. For a new campaign, explicitly allocate its budget:

```bash
repo2rlenv campaign init ./workspace/my-campaign --budget-usd 25
```

Review a task and a previously collected solver trial:

```bash
repo2rlenv quality run ./tasks/example \
  --campaign ./workspace/my-campaign \
  --out ./workspace/reviews/example \
  --rollout ./jobs/example/trial/result.json \
  --review-model openai/gpt-6-astra
```

`--baseline` and `--oracle` also accept existing evidence. Each argument accepts
one owned trial receipt/directory, one Harbor trial `result.json`, or a single-trial
job directory. A multi-task job is ambiguous and is rejected. Native Harbor results
must match the input's Harbor checksum. Owned receipts must match its bundle hash
and the collected result hash. These are content checks, not cryptographic
attestations that an external caller ran an honest experiment.

Run the controls, review, probes, solver and up to two targeted repairs:

```bash
repo2rlenv quality run ./tasks/example \
  --campaign ./workspace/my-campaign \
  --out ./workspace/reviews/example-repaired \
  --repair --provider modal \
  --runtime-wheel ./dist/repo2rlenv-0.8.8-py3-none-any.whl \
  --review-model anthropic/claude-sonnet-4-6 \
  --repair-model anthropic/claude-opus-4-6 \
  --solver-model anthropic/claude-sonnet-4-6 \
  --max-repairs 2 --max-spend-usd 15
```

Switch to `--provider daytona` after installing `--extra daytona`. Alternatively,
pass `--worker-receipt` for an existing running worker in the same campaign. The
component terminates workers it creates; callers retain ownership of supplied
workers. A completed worker's compute reservation remains held until reconciled
through `repo2rlenv campaign settle`; model costs use recorded provider usage and
LiteLLM estimates. The budget is an accounting limit, not a provider-enforced bill cap.

Use `--run-rollout` without `--repair` to evaluate without editing. Supply an
existing rollout alongside either flag to avoid repeating it on the original
revision. A changed task always needs fresh controls, probes and a fresh rollout.

`--resume` reuses completed, identical model requests and trial evidence. It refuses
changed inputs, execution settings, prompts or evidence. Interrupted/uncertain
provider effects require reconciliation; they are never automatically retried.
`repo2rlenv quality show OUTPUT_DIR` reads the report without model/cloud calls.
Both commands support `--json`; ordinary runs show stage progress and criterion scores.
Credentials come from the usual environment or `.env`; `--env-file PATH` can load
the original checkout's credentials when running from another worktree. Keys are
not copied into task bundles or model request receipts.

## Models and efficiency

Review, repair, escalation and solver models are independent `provider/model`
settings. The default review/solver is the previously tested Sonnet 4.6; repair uses
the reviewer unless overridden. `--escalation-model` makes at most one stronger
attempt when structured output or evidence retrieval remains unresolved. This is
explicit escalation, not a silent retry of a failed provider request.
An invalid JSON/text patch gets at most one separately metered correction with the
validation error and actual source excerpts. It consumes no execution revision
until a complete patch applies. Unknown provider outcomes are never retried this way.

Current model examples include `openai/gpt-6-astra`, `openai/gpt-5.6-terra`,
`openai/gpt-5.6-luna`, `anthropic/claude-sonnet-5` and `anthropic/claude-opus-5`.
IDs are passed through, not selected from a frozen allowlist. Account access and SDK
compatibility still matter. See the official [OpenAI model catalog](https://developers.openai.com/api/docs/models)
and [Anthropic model catalog](https://platform.claude.com/docs/en/models/overview).

The shared OpenAI client sends `max_completion_tokens` for current GPT reasoning
models and omits unsupported temperature parameters; automatic SDK retries are
disabled. Every model request has a durable request hash, response receipt and
campaign reservation. Unknown usage retains its hold rather than becoming zero cost.

Defaults: two repairs, two extra file-reading rounds, two semantic probes, 100,000
characters of document context, 6,000 output tokens per review/repair call, and
24 solver turns at 4,096 tokens per call. Context includes a file inventory, selected
task files, trace excerpts and explicit omissions. Reviewers can request up to six
400-line excerpts or bounded literal searches in each reading round. Python tasks
start with matching function/test excerpts and their module headers;
When trials are present, the initial task files use at most 35% of the document
allowance; execution evidence can fill the next 35%. Actual assertion failures
precede verbose baseline inventories and captured source. Probe scripts remain
individually readable instead of inflating every result summary. The remaining
30% is reserved for follow-up reads. Binary/large files remain recorded as
uninspected text; this is not a claim that every byte of every repository was reviewed.

## What the prompts ask

The full prompts ship with the package and are reproduced in the
[complete prompt and request-assembly reference](prompts/quality_loop.md):

| Stage | Input | Output | Prompt |
|---|---|---|---|
| Initial review | Public instruction, build inputs, private reference/tests, available controls/rollout | Task/verifier/leakage assessments, grounded issues, bounded read requests, semantic probes | [review.md](prompts/quality_loop.md#reviewmd) |
| Evidence read / escalation | Same evidence pack plus requested excerpts or protocol correction | Completed grounded review | Same review prompt |
| Post-execution review | Task plus actual control/probe/solver results, logs and captured artifacts | Legitimate success/failure, task defect, grading shortcut, infrastructure issue or insufficient evidence | Same review prompt |
| Repair | Grounded issues, execution failures, exact files and retained probes | Exact text replacements, optionally a corrected valid-alternative probe, and rationale | [repair.md](prompts/quality_loop.md#repairmd) |

Every citation must quote text actually supplied to the model. Scores range from
0–4 and are descriptive; code derives the final disposition from evidence. A
model's declaration of success cannot override a failed negative control or probe.

Semantic probes create private variants: first run the reference, then install a
plausibly wrong submission or a valid alternative. Instruction, environment and
verifier files stay byte-identical. A completion marker and oracle exit code
distinguish probe-installation failures from verifier rejections. Wrong-solution
probes are retained across repairs. An alternative can be corrected only after a
grounded diagnosis identifies a bug in that probe; its name, kind and requirement
focus must stay the same. Original probe receipts remain available. A probe-only
repair reuses the unchanged task's controls and rollout, then replays both probes.
Their correctness still needs grounded
model review; they are not a comprehensive adversarial suite or privilege test.

Explicit lazy-output and numeric-tolerance requirements select focused probes.
A different wrong implementation cannot satisfy those coverage obligations. This
addresses a measured calibration failure: two generic probes initially missed the
R2E `collapse` verifier's missing laziness check. Probe focus is still a narrow
heuristic, not a complete natural-language requirement extractor.

Pass `--probes FILE.json` to retain known counterexamples or valid alternatives from
an earlier review. The manifest contains `bundle_hash` and a `probes` list; each
probe has `name`, `kind`, `focus`, `rationale`, `evidence` (`path`/`quote`) and `script`.
Its hash must match the task and its count must fit `--max-probes`. Known probes
are replayed against every revision. Fresh suggestions cannot displace them; only
the diagnosed valid-alternative correction described above can change a known probe.

## Live validation and calibration

The September 12 canaries used real model APIs and Modal-hosted Harbor trials.
They tested this component, not a new corpus-wide acceptance pass.

| Input | Models exercised | Observed result |
|---|---|---|
| Existing SCALER task | GPT-6 Astra reviewer | Structured review completed and flagged an unestablished numerical-tolerance contract; no new task execution or repair. |
| Small iterable-sum coding task with weak tests and a broken verifier image | Sonnet 4.6 review/solver, Opus 4.6 repair | One repair fixed the image packaging and added behavioral coverage. Fresh baseline 0, reference 1, wrong solution 0, valid alternative 1, Sonnet 1; `usable`. |
| Existing R2E `collapse` task, early generic probes | Sonnet 4.6 | **False acceptance during calibration:** ordinary wrong/valid implementations missed the promised lazy evaluation. This motivated explicit requirement-focused probes. |
| Same R2E task, focused probes | Sonnet 4.6 | The eager implementation earned 1, demonstrating a verifier defect. A generated test repair made it earn 0 while the reference still earned 1. A separate defective alternative remained unresolved, so the run was not selected. |
| Same R2E task, full repair attempt after context correction | Sonnet 4.6 review, Opus 4.6 repair | The reviewer identified the actual `base_type` failure. A generated laziness test had an undefined import; fresh oracle execution rejected it. The following patch did not match its target. The run ended `needs_evidence`, motivating bounded patch correction and module-header context. |
| Previously generated R2E laziness revision, with retained probes | GPT-6 Astra review/repair, Sonnet 4.6 solver | One additional revision clarified outer-container atomicity, strengthened partial-consumption grading and corrected the recursive alternative. Fresh baseline 0, reference 1, eager probe 0, recursive alternative 1 and Sonnet 1; final review classified the rollout as legitimate and returned `usable`. |

The iterable canary accounted for $0.224319 in model usage and a conservative
$1.038314 compute/build estimate. These are small-case measurements, not a general
per-task price. Cloud accounting uses the [Modal sandbox rate card](https://modal.com/pricing)
and recorded worker duration plus a $1 image-build allowance; it is not a provider
invoice. Existing SCALER evidence was reused, so that review's $0.583783 model cost
does not include its earlier execution.

The final R2E run used $3.477980 in API calls and a $1.075085 conservative
compute/build estimate. It started from an earlier generated repair and known
probes, so this is an incremental result, not a one-shot success-rate measurement.
Across all development canaries, recorded API usage was $8.218809 and conservative
cloud/build accounting was $6.274614 ($14.493423 combined). All six created workers
were terminated and their holds reconciled. Historical campaign reservations are
separate and unchanged.

Final component verification: 869 local tests passed, one opt-in GitLab test was
skipped, and external/live test suites were excluded from the local run. GitHub CI
passed lint, Python 3.12/3.13/3.14 tests, optional owned/Harbor contracts and package
builds. No original export was replaced or newly admitted by these canaries; all
291 original bundle hashes still match their recorded identities.

Calibration also caught two controller issues: current OpenAI models need the
completion-limit parameter supported by their API, and long baseline result files
must not hide later probe failures. Both have regression coverage. Invalid quotes,
truncated responses and unresolved execution evidence stop the loop instead of
becoming accepted tasks. Daytona uses the existing adapter but was not exercised
in these component canaries.

## Output and acceptance

| Status | Meaning |
|---|---|
| `usable` | All three review criteria pass; initial state fails; reference passes; wrong-solution probe fails; valid-alternative probe passes; blind rollout is reviewed as a legitimate success or failure. |
| `reviewed` | Review is sound but some execution evidence is absent or unresolved. This is not validated acceptance. |
| `needs_repair` | Concrete task, reference, packaging or verifier defects remain. |
| `needs_evidence` | Parsing, evidence binding/retrieval, provider execution or infrastructure needs diagnosis. |
| `budget_exhausted` | Per-run or campaign allowance prevented the next effect. |

Exit codes are 0 for `usable`/`reviewed`, 1 for other completed dispositions, and 2
for invocation/setup failures. Automation requiring validated tasks must check
`status == "usable"`, not only the process exit code.

```text
OUTPUT_DIR/
  result.json                 # Current disposition, scores, evidence, cost and task path
  run.json                    # Input/options/evidence identity
  events.jsonl                # Durable progress
  calls/                      # Exact prompts, responses and budget receipts
  reviews/                    # Structured review per revision/stage
  revisions/r0/TASK/           # Original snapshot
  revisions/r1/TASK/           # New Harbor task; adjacent repair.json has lineage
  probes/r0-probe0/TASK/       # Private control variant; never a published training task
  trials/                     # Harbor results, trajectories and artifacts
  workers/                    # Owned worker recovery/cleanup receipts
```

## Python integration and current boundaries

```python
from pathlib import Path
from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.quality.loop import LoopOptions, QualityLoop

loop = QualityLoop(
    LoopOptions(), Path("workspace/review"),
    BudgetLedger(Path("workspace/campaign/budget.sqlite3")),
)
result = loop.run(Path("tasks/example"), rollout=Path("jobs/trial/result.json"))
```

For execution, supply `RemoteTrials` as `trial_runner`, or assign it to `loop.remote`
using `loop.budget`. Model/trial adapters are injectable for tests and Tasksmith
integration; this layer does not depend on any recipe or upstream research package.

The initial remote adapter supports Linux Dockerfile tasks with `no-network`,
including Harbor's separate verifier environment used by owned recipes. It does
not run multi-service, Windows, step-based or online tasks. Repairs are bounded text
edits to instruction/environment/solution/tests; task configuration, binary assets
and externally hosted artifacts require a separate explicit change. Invalid or
missing task contracts cannot be silently invented. Artifact visibility depends on
the input task's Harbor collection configuration; missing final files are a review
limitation, not evidence that no exploit occurred.
