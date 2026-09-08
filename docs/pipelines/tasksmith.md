# Tasksmith (experimental)

Tasksmith constructs and repairs a verifiable environment for each supplied GitHub
PR. One canonical PR identity owns one task lineage; revisions and retries never
increase the distinct-task count. A failed generation stage remains evidence about
that attempt, rather than becoming a reason to remove the PR from the denominator.

The initial implementation combines a persistent LangGraph workflow, Pi or
OpenCode authors, independent structured reviews, and remote Harbor trials. Its
five-PR proof of concept is pending complete execution and evidence review. No
admission or yield claim follows from the implementation, static planning, or a
provider fixture passing.

## Invocation

Install the optional execution dependencies:

```bash
uv sync --extra curation
```

Tasksmith uses the existing native author adapter installation described in
[dynamic curation](dynamic_curation.md). Model and provider credentials remain in
the controller environment, outside the task bundle. `provider` selects `modal`
or `daytona`; `author_runtime` selects `pi` or `opencode`.

Supply a JSON `TasksmithConfig` and a JSON panel. A simple panel contains PR URLs:

```json
[
  "https://github.com/example/project/pull/123",
  "https://github.com/example/project/pull/456"
]
```

Research manifests with `{"panel": [{"url": "...", "...": "..."}]}` are also
accepted. Source metadata and research observations remain attached to that input.
Duplicate canonical PR identities are rejected; the CLI does not silently dedupe
or replace sources. The execution workflow freezes the source and panel identities.

Validate the local configuration and count panel sources:

```bash
repo2rlenv tasksmith \
  --config tasksmith-config.json \
  --panel tasksmith-panel.json \
  --out workspace/tasksmith-run \
  --plan
```

`--plan` makes no model, GitHub, or provider calls. It does not write run artifacts,
change the ledger, verify source pins remotely, or assess task suitability.

Exercise both providers with the bounded standalone CPU fixtures:

```bash
repo2rlenv tasksmith \
  --config tasksmith-config.json \
  --out workspace/tasksmith-provider-checks \
  --check-providers
```

This command performs **metered remote work** on Modal and Daytona. Its result
describes dependency build, execution, transfer, network and cleanup observations.
These fixtures do not establish a PR's correctness or complete Harbor grading
conformance. A failed or incomplete provider check returns a nonzero exit status.
`--plan` and `--check-providers` are mutually exclusive.

Run the panel after the execution prerequisites are satisfied:

```bash
repo2rlenv tasksmith \
  --config tasksmith-config.json \
  --panel tasksmith-panel.json \
  --out workspace/tasksmith-run
```

Use the same configuration, panel and output directory for recovery. There is no
CLI flag that silently rerolls failed reviews, changes source pins, resets a
deadline, or discards uncertain effects. A completed controller run can contain
zero admitted tasks; read the per-PR result and exact evidence.

## Configuration and retained budget

The required JSON fields are `ledger_path`, `ledger_limit_usd`, and `campaign_id`.
The configuration also freezes campaign/candidate and stage limits, validation
reserve, author/reviewer/solver model IDs, runtime, provider, revision allowance,
timeouts and concurrency. Unknown fields are rejected. Use absolute ledger paths
in retained configurations so changing the working directory cannot select a
different file.

This POC remains under the original **$500 combined authorization**, including
earlier campaigns and outstanding reservations. A new output directory is not a
new allowance. Reconcile retained ledgers first; configure the authoritative
primary ledger ceiling after accounting for any commitments stored elsewhere.
Campaign and candidate limits are nested limits on that ledger, not independent
funds. Tasksmith's operation journal records ledger identity and reservation IDs;
it contains no parallel model-spend ledger.

Estimates, metered charges and uncertain reservations must remain distinguishable
in the result. Provider fixture work, failed builds, author repairs, reviews,
solver trials and cleanup all belong in the accounting. Configuration defaults
are implementation defaults, not approval to allocate new spending.

## Contracts and admission

The task, episode, verifier, oracle and materialization contracts bind source pins,
public requirements, editable and collected paths, reset/termination rules,
protected expectations and the execution origin. The first execution profile is
source-based Python on a CPU in a single remote container. Native source rebuilds,
GPU, multi-service and performance profiles need additional conformance evidence;
the presence of fields describing those modes does not establish support.

Bootstrap failures have separate repair paths. Missing dependencies preserve the
checks while allowing one dependency rebuild. A demonstrated pytest selector error
allows one selector correction after complete pinned-source and collection capture;
an import failure cannot be reclassified merely because pytest also reports no
collectors. Source files and collection reports use bounded file transfers verified
by size and SHA256, so clipped shell output is never parsed as complete evidence.

A faulty generated Python smoke can receive one independently reviewed correction.
The author must cite the failed command and source observations, and the reviewer
must establish that the replacement tests the intended capability. The controller
changes only that command and reruns all readiness checks on the same dependency
image and pinned reference. Rejected, uncertain or genuine reference failures
remain failures; they cannot authorize weaker tests or another diagnosis attempt.

The selected developer request is reviewed before verifier construction. A
separate editor can make at most two corrections using only public text, the
pinned base file inventory and required review feedback. It has no shell, reference
patch or private verifier. Once approved, the request is frozen during construction:
the coding author builds tests and the oracle against it, and any attempted rewrite
is rejected and restored. The final emitted task still receives independent
instruction and verifier reviews. Missing review evidence remains incomplete.

Solver and grader run with an offline runtime policy and distinct grading trust
boundaries. Submitted code executes without protected assertions or reward-writing
authority. The trusted grader interprets bounded observations. Builds, generated
code, tests and solver submissions execute remotely, never in the controller.

Episode reward and environment admission are separate:

- Completed protected behavioral success earns reward 1.
- Demonstrated submission failure earns reward 0 only with healthy environment,
  complete collection and verified materialization evidence. Invalid submitted
  source can fail protected behavioral checks without invalidating the environment.
- A learner's failed observation probe is retained as diagnostic evidence while
  the complete protected test suite still determines reward. Successful, differing
  baseline/reference observations remain mandatory for environment admission.
- An author oracle or control script that exits unsuccessfully produces a
  repairable construction error when its execution receipt and cleanup are
  verified. It earns no behavioral reward. Literal shell changes into private
  author directories are rejected during emission; episode scripts use `/workspace`.
- Provider, missing-transfer, stale-origin, harness and incomplete-review failures
  are incomplete evidence, not training negatives.

The independent quality report covers eight dimensions: useful scope, instruction
sufficiency, verifier correctness/coverage, valid-alternative tolerance, oracle
validity, isolation/leakage, reproducibility/materialization, and trajectory
findings. Every applicable required dimension must pass with score 3 or 4 and
exact-revision evidence. A high average never compensates for a failed dimension
or an unresolved material finding. Not-applicable outcomes require an explicit
controller policy exception with cited reasons and cannot waive missing trials.
Difficulty and observed solver success are descriptive; expert-review status is
reported separately.

The controller freezes the required control/trial inventory before admission.
It includes repeated oracle validation, appropriate baseline and incorrect/valid
alternatives, tamper and independent challenges, ordinary Sonnet and Opus trials,
and a distinct adversarial audit. Static suggestions and author smokes do not
replace that evidence.

## Recovery and auditability

LangGraph checkpoints retain workflow state. A separate SQLite journal retains
immutable stage input/policy identities, claims, provider IDs, receipts and artifact
write intents. A per-PR lease prevents competing controllers from starting the
same work. Lease expiry makes unfinished effects uncertain; it does not terminate
their remote resources. Reconcile those IDs and cleanup obligations before another
attempt. An absolute original deadline still applies after recovery.

Coding workers do not inherit the outer workflow's checkpoint namespace. Worker
artifacts and explicitly supplied continuation state govern their recovery; an old
nested checkpoint cannot substitute for a fresh artifact validation callback.

`tasksmith.finalize.prepare_finalization` provides a separate recovery path when
all trials finished but the final review did not. It verifies the committed dossier,
task bytes, collection inventories, source witness, provider cleanup and original
cost/deadline receipts without executing trials. `finalize_review_only` owns one
linked review operation and retains the unfinished parent record. Explicit
publication holds remain binding even if the model gives a passing score.

Normal validation and review-only recovery deliver a complete dossier in one request using
`tasksmith.inline_review.final_review_inline`. A reversible projection shares repeated
text and source edits while retaining raw artifacts and exact reconstruction hashes.
Length-framed evidence avoids another layer of JSON escaping. Delivery receipts
identify the complete supplied text; they do not establish reasoning quality. All
eight criterion names are explicit in the tool schema. A truncated, malformed or
wrong-rubric response remains incomplete. Previous review charges still count toward
the same allowance. Normal validation commits the original fourteen evidence roles,
their reversible delivery projection, and the exact request/response receipts even
when the reviewer fails. Recovery checks those additional receipts when present;
older paged-review records remain supported. A completed review requires reconciliation,
not another model call. This API does not retry trials or replace missing evidence.

CPU/GPU capability errors retain a profile-mismatch receipt. For the supported TRL
configuration API, one independent source review can authorize CPU selection only
when the upstream constructor omitted `use_cpu`. This preserves precision, assertions,
source, dependencies and test commands. Explicit GPU choices or essential GPU semantics
stop the attempt. Approval permits full collection and execution on the same image;
every changed test anchor and unchanged readiness command must still pass. A failed
adaptation stops before another dependency attempt. This path has controller tests,
but has not yet been validated against the live TRL candidates.

Dependency repair requires successful collection
of every selected test module from clean pinned source before the final rebuild;
the fresh dependency image must still pass the original complete readiness commands.

If a controller defect is diagnosed after a candidate has stopped, the Python
`Candidate.run(revision_request=...)` interface records the current task digest,
concrete diagnosis, retained evidence and repair feedback before scheduling a new
revision. It preserves the old validation result and does not rerun an accepted
candidate. Outstanding stages/resources must be reconciled first; original
revision, time and budget limits still apply. Replaying the same request resumes
that repair. Every new revision must pass the full validation sequence.

Artifacts are published under content-derived paths without overwriting existing
bytes. Interrupted writes recover only their recorded hash-bound intent. Reusing a
completed operation verifies its artifacts again, so changed or missing evidence
cannot silently advance the workflow. The journal selects at most one exact
revision per PR and rejects changes to an existing selection's evidence.

Full transcripts, source/procedure hashes, all revisions, controls, trial records,
quality reports, unresolved reasons and accounting remain available for independent
review. Keep private reference and audit material outside solver-visible exports.
See [RFC 0012](../rfcs/0012-tasksmith.md) for the design and validation sequence.
