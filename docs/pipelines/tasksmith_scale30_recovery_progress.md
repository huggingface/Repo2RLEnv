# Tasksmith recovery: offline assets and bounded repairs

September 13, 2026, 13:27 UTC checkpoint. The user authorized an additional $100,
raising the total campaign cap to **$600**. The authorization is recorded without
resetting any charged operation or unresolved reservation.

**19 of 30 tasks have completed acceptance.** This includes the ten original tasks,
the four additions in the preceding delivery, Transformers #35669 (Helium), and
TRL #6150 (KTO margins), TRL #5501 (logging), TRL #5575 (CUDA activation memory),
and Accelerate #3142 (scaler dispatch). Recovery and generation remain in progress. The existing
14-task archive remains an earlier delivery; a running task is not included in
the accepted count. The machine-readable checkpoint is
[tasksmith-scale30-recovery.json](evidence/tasksmith-scale30-recovery.json).

## Changes to the pipeline

```mermaid
flowchart TD
    PR[Pinned PR and source inventory] --> Profile[Inspect dependency versions and selected test bodies]
    Profile --> Local[Small real models and local datasets]
    Profile --> Assets[Explicit Hub file list, commit and byte limit]
    Assets --> Metadata[Check every file size and revision before downloading]
    Metadata --> Cache[Remote source-free dependency and asset layer]
    Local --> Ready[Offline merged-head readiness]
    Cache --> Ready
    Ready --> Design[Human task request and independent behavioral verifier]
    Design --> Contrast[Original source fails; fixed PR reference passes]
    Contrast --> Review[Review instruction, verifier, fixtures and leakage]
    Review --> Controls[Wrong solution and valid alternative controls]
    Controls --> Rollout[Blind Sonnet rollout]
    Rollout --> Judge[Review trace, submitted changes and execution]
    Judge -->|Concrete defect| Repair[Consolidated repair: default maximum 3 rounds]
    Repair --> Contrast
    Judge -->|Supported acceptance| Task[Harbor task plus evidence]
    Judge -->|Unresolved defect or exhausted allowance| Hold[Retain explicit unfinished diagnosis]
```

The author can now declare `hub_assets` in a repository profile. Each entry specifies
one public model/tokenizer repository, an immutable commit, exact data filenames,
a maximum download size, and its purpose. Assets are fetched during remote image
construction. The layer is independent of repository source; revisions and file
lists affect its cache identity, while explanatory wording does not.

Both image readiness and exported tasks use this recipe. The private grader preserves
the image's cache paths and offline flags when switching to its unprivileged child.
It does not forward model-provider credentials. CPU and CUDA images retain their
existing separate build and execution paths.

The review-and-repair component defaults to three repairs, configurable through
`max_repairs` or the existing `--max-repairs` option. Prompts expose the remaining
rounds and request one consolidated correction of known defects. Wrong and valid
controls remain attached to subsequent revisions. The controller can deduplicate
identical proposed controls, rename a colliding probe without changing its script,
and resolve an omitted verifier directory only when one existing file contains
the exact requested old text once. These mechanical corrections are recorded;
ambiguous edits and changes to protected source/reference still fail. A shortened
test-result key can be resolved only when a complete JSON document contains one
matching test name with the identical value; the recorded citation then uses the
actual document bytes. Prose claims are not inferred or rewritten.

A complete, metered author response that is truncated or contains no usable output
can receive one new attempt. It shares the original stage's cost, turn and deadline
limits. Partial tool arguments are not executed. Unknown transport outcomes retain
their cost reservations and are not replayed automatically. Native Modal directory
uploads now verify the archive checksum before extraction and permit up to three
transfers of the same temporary file. A corrupt transfer does not trigger a new
sandbox allocation or solver run; cancellation is not retried.

Repository semantic controls now hash their explicitly collected mutable submission
before and after the probe. A script that only runs extra assertions, writes outside
the collection boundary or rewrites identical bytes cannot count as an installed
control. The audit does not import target code. Changed bytes alone do not establish
a meaningful alternative; the reviewer still checks semantics. Existing frozen
receipts are retained and are not retroactively relabeled by this new guard.

Bootstrap and private grading clear repository pytest `addopts` before applying
explicit test selections. This prevents optional coverage plugins from breaking an
isolated run with plugin autoload disabled. The native adapter releases a solver
hold only when all allocation receipts prove a pre-dispatch reservation denial and
Harbor explicitly records no agent execution or result. Other unknown outcomes keep
their reservations.

## What the remote checks establish

| Check | Observed result | Limit of the evidence |
|---|---|---|
| Pinned Qwen tokenizer preparation | Passed on Modal; second build reused the exact image | Does not establish every tokenizer or checkpoint is compatible |
| Offline tokenizer use | Passed with Docker networking disabled | Selected tokenizer files only; no pretrained weights downloaded |
| Unprivileged grader cache access | Passed as UID 1001 with the grader's child environment | Cache availability is separate from correctness of a repository's tests |
| Helium | Accepted with baseline/reference, four retained semantic controls and reviewed rollout | One frozen PR and tested small configurations |
| KTO margins | Accepted with local fixtures, semantic controls and reviewed rollout | No hosted dataset or pretrained checkpoint required |

The tokenizer-dependent TRL #5791 run exposed a second, independent readiness problem:
two upstream tests call the new tokenizer parser without its required `prefix`.
The two actual TRL parsing tests pass offline. The next attempt retains those tests
and requests additional prefix-aware behavioral coverage. The original failed
readiness receipts remain available.

The ongoing GPU investigations also demonstrate why an oracle pass and a numerical
quality score are insufficient. Accelerate #3142 originally accepted a control
that discarded FSDP dispatch information. Accelerate #3720 used incompatible
distributed fixtures. TRL #5575 now isolates the intended
training workload and rejects retained dense vocabulary activations. Accelerate #3142
now rejects both version-boundary and FSDP-dispatch regressions. Both have reviewed
blind rollouts. The mesh task remains unfinished. Separately, final verifiers for
TRL #5349 and Accelerate #4015 initially used CPU tensors or mocked sharding despite
GPU resource declarations; real CUDA and distributed fixtures are now being checked.
Raising thresholds to match a failing reference is not an acceptance method.

## Budget at the checkpoint

**$554.05 accounted, $33.09 reserved, $12.86 unreserved.** The accounted figure
includes the previously recorded $22.43 external cost. Compute amounts are
conservative estimates, not provider invoices. Reservations include unresolved
historical operations and currently owned workers/trials; they are not all charges.
Every new operation is checked against the campaign ledger and its recovery/run
allowances. No target images, models or repository tests execute on the local machine.
