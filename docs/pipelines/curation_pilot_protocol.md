# Controlled curation pilot

The five-PR pilot measures automatic yield under a fixed policy. It follows a
read-only audit of retained failures and the [regression corpus](curation_regressions.md).
Earlier manually repaired tasks are useful evidence, but do not count as automatic
successes in this experiment.

1. Screen a catalog before launching authors. Record exclusions, uncertainties,
   selection order, source base/head commits, and the screening rule. Queued PRs
   are not attempts. Historical author contexts can disqualify freshness without
   proving that the current pipeline attempted the PR.
2. Freeze five sources, configuration, and the harness digest in a protocol JSON.
   Use the existing production ledger. The batch has a $40 commitment ceiling and
   each slot has $8; reservations, estimated cloud cost, and metered model costs
   share these limits. Prior costs remain charged. These are accounting ceilings,
   not a claim of reconciled provider invoices; settlement overruns remain visible.
3. Authors explore remotely and write a verification design before implementing
   assertions. Every requirement names independent expected results, tests, a
   plausible wrong implementation and a permitted alternative. Structural checks
   enforce those mappings before paid review or task execution. The verifier
   reviewer reads the plan and actual tests; a plan alone proves nothing.
4. Each slot permits an initial submitted task and one autonomous repair.
   Distinct finalized task digests count across validation tool calls and author
   rounds; structurally invalid submissions count too. Resubmitting identical
   content does not consume another draft. Exports that cannot safely be hashed
   (such as linked or oversized files) each consume a submission. This limits submitted candidates, not
   ordinary edits during initial authoring. No manual rescue counts toward yield.
5. Keep structural and static reviews, baseline/reference repetitions, negative
   and equivalent controls, Sonnet/Opus attempts, and adversarial attempts. A
   solver failure needs attribution; task validity and task difficulty are
   reported separately.
6. Independently inspect autoaccepted tasks and their traces. Scale only after
   at least three of five are selected, with no known material verifier defects,
   and a recorded assessment of cost and remaining budget. This is an operational
   gate, not statistical evidence of general reliability.

The controller never automatically restarts an interrupted slot. It records the
interruption, retains outstanding charges, and continues only unstarted slots.
Changing the frozen runtime or configuration is rejected. Moving to a new output
directory cannot reset a pilot that already has charges. All five slots remain in
the denominator, including deferrals, budget stops, and infrastructure failures.
No replacement is made after observing author or solver outcomes.

```bash
repo2rlenv curation-audit --workspace workspace --seeds seeds.md \
  --ledger workspace/production/budget.json \
  --selection workspace/curation-final-selection.json --out workspace/audit
repo2rlenv curation-pilot --protocol workspace/pilot-protocol.json \
  --out workspace/pilot
```

The protocol contains `id`, `runtime_digest` (from
`repo2rlenv.curation.pilot.runtime_digest()`), `ledger`,
`production_limit_usd`, `sources` (five resolved PR records), and `config`.
The configuration requires `target=5`, `budget_usd=40`, `max_candidate_usd=8`,
`max_revisions=2`, `max_candidate_drafts=2`, `require_verification_plan=true`,
and both specification and verifier reviews enabled. Keep the screened source
records and protocol alongside the resulting manifest for reproducibility.

Cloud builds and task execution use Modal. Solver and grader environments remain
offline, with separate protected assertions and no PR history or solution data
in the solver environment. Other author adapters remain available; the pilot
holds its chosen adapter fixed instead of changing it after an unfavorable result.
