# RFC 0012: Tasksmith

**Status:** accepted; experimental implementation undergoing validation

**Created:** 2026-09-08

## Summary

Tasksmith maintains one resumable PR-to-environment lineage for each input PR.
It combines remote construction and protected execution with independent evidence
review, retaining unsuccessful attempts and repairing diagnosed defects without
lowering the admission bar or multiplying revisions into distinct tasks.

## Motivation

The previous curation work exposed gaps between a passing reference, a favorable
review, and a useful training task. Missing interactions, biased assertions,
incomplete source collection and uncertain remote effects require explicit
contracts and targeted recovery. Workflow checkpoints alone cannot restore a
remote process or establish that a provider-side effect did not happen.

Tasksmith reuses source ingestion, the metered model bridge, native author
adapters, structured reviewers, the existing `Budget`, and protected probe
execution. It adds a narrow orchestration and contract layer instead of replacing
those mechanisms. Keeping this work entirely in the previous campaign driver
would entangle new lineage/effect identities and provider choices with historical
admission and recovery policies; frozen historical evidence must remain interpretable.

## Design

### Input

`repo2rlenv tasksmith --config CONFIG --panel PANEL --out ROOT` consumes an explicit
GitHub PR panel and strict `TasksmithConfig`. Source pins, the configuration, the
original deadline, and existing ledger identity are retained. `--plan` validates
local inputs without external calls; `--check-providers` explicitly performs
metered fixtures on both providers. This is a separate command, not an additional
`generate --pipeline` registration.

### Algorithm

1. Freeze canonical PR/source identities and understand the useful observable change.
2. Bootstrap dependencies remotely and perform cheap readiness observations.
3. Select a coherent task and verification approach, then freeze public and episode contracts.
4. Construct the verifier, reference and complete collection/materialization procedure.
5. Review public comprehension, leakage and verification; exercise independent challenges.
6. Run required deterministic controls and fresh ordinary/adversarial trials.
7. Diagnose submissions and trajectories; repair reproduced specification or verifier defects.
8. Admit one exact revision only after every required quality and execution gate passes.

LangGraph coordinates coarse stages. Authors use Pi or OpenCode through the same
remote-tool boundary inside those stages. The operation journal uses SQLite
transactions and per-PR leases; large evidence artifacts remain outside graph state.
The existing budget ledger remains the sole authority for reservations and charges.

### Output

The trusted emitter produces explicit Harbor task schema 1.4 with public
instruction/environment files, private reference solution, and protected grader
files. Typed contracts record task requirements, episode/reset behavior, verifier
expectations, oracle adaptations, and materialization. Collection must include
every allowed edit and new helper without overlapping destinations. Source edits
must become the executed artifact, with an observed origin check.

Stage receipts bind revision, input and policy hashes, attempt/effect identity,
deadline, ledger references and external IDs. A durable artifact intent precedes
exclusive content-addressed publication; recovery verifies bytes before committing
or reusing them. One release selection belongs to each canonical PR, regardless of
the number of retained revisions.

## Verification

Initial episode reward is deterministic and binary. Reward 1 requires completed
protected success. Reward 0 requires a demonstrated submission failure under a
healthy environment, complete collection and verified materialization. An invalid
submitted program can fail a healthy pinned build; missing files from a broken
transfer or a stale installed binary invalidate evidence instead.

The controller freezes the required trials, expected control outcomes and exact
model identities. Required controls include appropriate baseline/repeated oracle
observations, meaningful incorrect and valid alternatives, protected-runner tamper
checks and a fresh independent challenge. Ordinary Sonnet/Opus and adversarial
trials follow. Legitimate adversary success is distinguished from a reward hack
through its observations and submission.

Admission requires all eight specified quality dimensions: useful scope,
instruction sufficiency, verifier correctness/coverage, valid-alternative
tolerance, oracle validity, isolation/leakage, reproducibility/materialization,
and trajectory findings. Each applicable required dimension must pass with
score 3 or 4, complete current-revision evidence and no unresolved material
finding. Exceptions to applicability require explicit controller policy and
evidence. Difficulty is descriptive, and optional polish does not become a new
public requirement. A successful review wrapper or high aggregate is insufficient.

## Anti-contamination

Use clean dependency/source layers rather than deleting a reference from an image
that once contained it. Keep reference patches, private checks, credentials,
answer-bearing caches and audit material out of the solver bundle. Separate
grading is required, but is not itself proof of isolation: submitted code must
run without access to protected expectations or reward-writing authority.

Runtime network policy is offline for both solver and grader. Approved assets
are hydrated by exact identity before execution. Provider selection is explicit;
there is no local-Docker or alternate-provider fallback. All generated code and
task execution remain remote.

## LLM use

Models investigate, author, criticize and diagnose; they do not compute the initial
episode reward. The initial independent structured review loop is reused because
it supports the required validated output options. Prompt/model identities, tools,
turn bounds and deadlines are recorded per operation. An unchanged rejected review
is retained rather than rerolled for a favorable verdict.

There is no validated cost projection for 100 admitted tasks yet. The first
five-PR POC measures total cost, including unsuccessful construction and validation,
under the original combined $500 authorization and existing ledger commitments.
Nested campaign/stage allocations do not reset that ceiling. Unknown costs and
outstanding reservations remain visible.

## Yield and repo suitability

Initial validated support targets source-based Python CPU tasks in a single remote
container. No relevant upstream tests is a supported design branch, not an automatic
source rejection, but independent behavioral checks still must execute. GPU,
native rebuild, services and performance profiles need their own conformance
evidence; preserve an unsupported PR's actual scope and report the limitation.

The whole frozen panel stays in the denominator. Report distinct PRs, revisions,
stage outcomes, unresolved reasons, repairs, metered/estimated/reserved spend and
selected tasks separately. No expected yield is asserted before the POC completes.

## Dependencies

Reuse Harbor 0.20, LangGraph, the existing Pi/OpenCode adapters and `Budget`.
The curation dependency group now includes `langgraph-checkpoint-sqlite` and the
Daytona SDK; exact versions belong in the lockfile and run receipts. The operation
journal itself uses standard-library SQLite. Provider-specific resource receipts
remain separate from the typed task contracts.

## Alternatives considered

- Graph checkpoints without effect receipts cannot distinguish an unstarted call
  from a successful create whose response was lost. Preserve uncertainty and reconcile.
- A new budget database would risk losing retained spend. Reference the existing ledger.
- Releasing every accepted revision would inflate conversion yield. Select one revision per PR.
- Enabling installed/compiled/GPU profiles by configuration alone would overstate
  materialization fidelity. Require a source-change witness and profile validation first.

## Rollout plan

First test contract contradictions, concurrent claims, crash boundaries, uncertain
cleanup and artifact tampering with trusted local fixtures. Then exercise bounded
CPU provider fixtures and Harbor grading boundaries remotely. Run the fixed five-PR
POC, retain all outcomes and costs, and independently audit exact selected revisions.
Only expand toward 30 after measured evidence supports the same admission policy.
The complete real-PR POC and publication remain pending; no dataset is claimed here.

## Open questions

- How much autonomous repair is needed per admitted task, including failed stages?
- Which dependency cache reuse survives source/materialization invalidation checks?
- Which additional execution profiles can provide reproducible protected measurements?
- What disagreements remain between automated and expert review?

## References

- [RFC 0011: evidence-driven curation](0011-dynamic-curation.md)
- [Tasksmith usage and recovery](../pipelines/tasksmith.md)
- [Curation regression corpus](../pipelines/curation_regressions.md)

## Implementation

Implementation lives in `src/repo2rlenv/tasksmith/`, with the CLI exposed through
`repo2rlenv tasksmith`. Local contract/journal tests and mocked CLI tests are
separate from remote provider/PR evidence. There is no shipping release, published
reference dataset, or completed five-PR admission claim attached to this RFC yet.
