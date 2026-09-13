# RFC 0027: Harbor task review and repair

**Status:** implemented
**Created:** 2026-09-12

## Summary

Add one generation-independent component that accepts a Harbor task and optional
rollout evidence, reviews task/verifier quality, and makes bounded repairs followed
by fresh remote validation. Reuse the owned execution, immutable bundle, metered LLM,
campaign ledger and console primitives.

## Motivation

The 70-task quality pilots found material verifier defects even when both paired
controls and a solver passed. Several defects were small and reusable: stale-output
acceptance, missing iteration checks, format/tolerance mismatches and offline agent
dependencies. Repeating assistant-only inspection for every recipe will not scale.

Keep this separate from generation semantics and from the older strict admission
profile. Its pragmatic disposition is useful to every recipe and to Tasksmith;
neither another generator nor another research framework dependency is required.

## Design

The [component guide](../pipelines/quality_loop.md) specifies the full flow, CLI,
prompts, input/output contracts and limitations. Owned code determines execution
gates; configurable OpenAI/Anthropic models review semantics and propose text edits.
The exact instruction and public behavior remain authoritative. Scores never
override executable evidence, and a solver failure alone never demands task repair.

Original input is copied and hashed. Existing receipts must bind to the same
content. Model calls are bounded, metered and cached by exact request. New revisions
rerun controls, retained counterexamples/valid alternatives and a blind solver.
An alternative-probe implementation can be corrected after an explicit grounded
diagnosis; known wrong-solution probes cannot be removed. Probe-only corrections
reuse unchanged task controls. Failure logs receive context ahead of long baseline
inventories so the reviewer can diagnose the actual assertion.
Malformed structured/text patches receive at most one metered correction against
actual source; provider failures with uncertain effects are never retried this way.
Workers execute Docker and target code remotely on Modal or Daytona. Caller-owned
workers permit cache reuse across tasks; auto-created workers are cleaned up.

The CLI selects native Modal for supported GPU resource declarations. CPU tasks
retain the shared Modal/Daytona Docker-worker adapter. A completed native single-step
solver settles known usage before private verification. If only verifier allocation
is denied, collection-time hashes bind the submission, trace, file modes and
allocation receipts. Same-input resume may make one verifier-only continuation;
it preserves original model evidence and never invokes the learner again. Changed
inputs, unsealed historical receipts and uncertain effects require reconciliation.
The continuation uses the pinned Harbor 0.20 controller API in an isolated module.

Private PR context has a bounded initial excerpt with the full document available
through the existing evidence reader. A unique Markdown prose citation can receive
a recorded single-backtick correction when every other character matches; normal
grounding still applies. Neither mechanism changes the semantic quality judgment.

## Validation

- Unit tests exercise immutable repair, stale evidence, grounded citations, reward
  false positives, legitimate learner failures, oracle/probe installation errors,
  repair limits, context boundaries and budget retention.
- Current OpenAI model routes use the correct completion-limit wire parameter.
- Live canaries test actual provider responses and remote Harbor execution; results
  and limitations are recorded in the guide rather than extrapolated to corpus yield.
- Legacy strict acceptance tests remain unchanged.
- Native continuation tests use the real Harbor controller with model/cloud effects
  blocked, checking tampering, cancellation, single dispatch and retained learner usage.
  Saved-review replay reproduces the observed MetaCLIP citation failure. A fresh
  unattended remote campaign is required before reporting autonomous yield; the
  existing accepted-task count includes assistance.

## Tradeoffs

The component uses a small persisted loop, not another orchestration dependency.
Tasksmith's existing LangGraph can call it through the Python API. Semantic probe
selection is model-assisted and fallible; grounding and execution narrow that gap,
but do not prove arbitrary reward-hack resistance. Unsupported asset/configuration
repairs stop with explicit evidence needs. Model cost estimates and compute
reservations are distinct from final provider billing.

## Credits and implementation

Harbor provides the task format, trial execution and trajectories. The owned
reproduction pilots supplied the empirical failure cases; generation-method credits
remain attached to each recipe. No upstream research repository becomes a runtime
dependency. No new library dependency is introduced by this component.

- Component: `src/repo2rlenv/quality/loop/`
- CLI: `repo2rlenv quality run` and `quality show`
- Tests: `tests/test_quality_loop.py`
- Native recovery tests: `tests/test_native_verifier_recovery.py`
- Intervention status: [autonomy audit](../pipelines/tasksmith_autonomy.md)
- Evidence background: [quality pilot](../pipelines/quality_pilot.md)
