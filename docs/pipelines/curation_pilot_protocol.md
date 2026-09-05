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
3. A separate bounded author phase explores remotely and submits a verification
   design before implementation starts (at most 20 turns/$2 within the same budget).
   Its structured tool returns schema feedback without consuming environment drafts.
   Acceptance atomically stores the design and its source digest; a different PR
   cannot reuse that cache. The host places the validated plan in the task directory
   before invoking the implementation author. Every requirement names independent expected results, tests, a
   plausible wrong implementation and a permitted alternative. Structural checks
   enforce those mappings before paid review or task execution. The verifier
   reviewer reads the plan and actual tests; a plan alone proves nothing. Host-owned
   review context retains the screened scope and original design, so later omissions
   remain visible even when the author rewrites the final plan.
4. Each slot permits an initial submitted task and one autonomous repair.
   Distinct finalized task digests count across validation tool calls and author
   rounds; structurally invalid submissions count too. Resubmitting identical
   content does not consume another draft. Exports that cannot safely be hashed
   (such as linked or oversized files) each consume a submission. A failed final
   submission saves its feedback and terminates immediately, without inviting
   another repair. This limits submitted candidates, not
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

The first frozen pilot used commit `df18b52` before the separate design phase and
immediate termination were added. Its original runtime and outcomes remain retained;
later fixes do not retroactively change that experiment. A discovered Dockerfile
comment false positive is also corrected: ordinary full-line comments are excluded
from executable-input checks, while heredoc bodies remain conservatively inspected.

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

## First frozen pilot: observed results

The September 5, 2026 pilot at `df18b52` completed with **0/5 accepted** and
**$7.683947 recorded cost** (metered models plus estimated cloud use). Each fixed
slot exhausted its two submitted drafts. No candidate reached isolated Harbor
baseline/reference, solver or adversarial trials; author-sandbox smoke checks are
separate evidence. There were no manual rescues or replacement candidates.

| Fixed source | Recorded cost | Observed blocking failures |
| --- | ---: | --- |
| PEFT #2962 | $1.296 | Invalid control mapping, then tests unable to distinguish state-dict selection from config fallback. |
| TRL #6152 | $2.182 | Missing plan, then guard coverage missing false cases; a proposed precompute fixture had already failed in author smoke checks. |
| Diffusers #13921 | $1.574 | Harness falsely flagged `git clone` in a comment; the next submission still lacked its verification plan. |
| Accelerate #3142 | $0.863 | Inconsistent plan mappings; independent audit also found omitted call-site behavior required by screening. |
| TRL #6150 | $1.769 | Single paired-batch fixtures missed wrong running-average behavior; the revised export included Python bytecode that the text-only evidence reader rejected. |

All costs count, including work exported after the submission allowance was already
exhausted. Those third exports were blocked before validation and are not extra
evaluated repairs. A structural rejection does not establish that the source PR is
inherently unsuitable. TRL #6150's repaired author-side smoke checks are encouraging,
but cannot establish isolated task admission or model difficulty.

The scaling gate failed. The three earlier selected tasks remain a separate,
manually reviewed collection pending human benchmark review; this pilot added none.
The target of 30 selected tasks has not been achieved.

The next runtime adds the separate design phase, retained screening context,
immediate final-failure termination, Dockerfile comment handling, and reviewer
instructions distinguishing suggested fixtures from executed evidence. These changes
have automated coverage, but no new live pilot validates their effect on yield.
Bytecode/export hygiene remains a known follow-up: silently hiding binary evidence
from the reviewer would be unsafe. Before any new paid batch, define and test a
canonical export policy and replay the retained failure cases without model calls.
A subsequent fresh pilot must have its own frozen sources and cumulative budget;
this failed pilot must stay in the denominator of any aggregate report.

## Conversion policy: validity and construction are separate

New experiments can opt in to `submission_policy="conversion"` and
`acceptance_policy="validity"`. Existing configurations default to `legacy`; no
retained task is silently reclassified. The conversion pilot preserves the five
fixed sources and $40 total/$8 per-source ceilings, with four author rounds, three
complete semantic submissions (initial plus two revisions), and six failed
mechanical input attempts. Every attempt still consumes its normal model/cloud
budget. A stopped run is not evidence that its PR cannot become a useful task.

Mechanical validation happens before a semantic submission is counted or a reviewer
is called. The host restores a missing verification plan from the accepted design,
without inventing requirements or controls; incompatible mappings still fail. It
removes only generated `.pyc`/`.pyo` under `tests` and `solution` with a corresponding
regular Python source, recording paths and hashes outside the task before hashing
its canonical contents. Orphan bytecode, linked files and nonregular entries fail.
Other fixtures and source are retained, including files inside a cache directory.
Arbitrary binary review evidence is not silently hidden. Repeated identical invalid
inputs consume mechanical attempts, so this path cannot bypass bounded retries.

A complete task counts against the semantic allowance before static quality review.
Coverage defects, ambiguous instructions and failed execution controls continue to
block acceptance and consume that allowance. Every final failure retains its cause.
Counters are separate in `mechanical-submissions.json`, `submitted-drafts.json` and
`construction-accounting.json`. They measure construction behavior, not inherent PR
suitability. A deferral remains an author claim requiring independent assessment.

Under validity admission, intrinsic difficulty is descriptive. The seven remaining
rubric criteria are normalized to 100 for the configured acceptance threshold;
all their individual gates, blockers, reward-hack findings, repeated references,
wrong-solution controls, valid alternatives and solver-attribution checks remain.
Results record the active policy, validity score, historical eight-criterion score
and difficulty score. Recovery and publication verify that receipt against the
original policy. An easy task can pass; an incorrect or unfair task cannot pass
merely by being easy. Neither a clean export nor a valid design is task admission.

To use the conversion profile, include these fields in a new frozen protocol's
`config` alongside the pinned sources, models and unchanged pilot budget fields:

```json
{
  "submission_policy": "conversion",
  "acceptance_policy": "validity",
  "max_candidate_drafts": 3,
  "max_mechanical_submissions": 6,
  "max_revisions": 4,
  "require_verification_plan": true,
  "specification_review": true,
  "verifier_review": true
}
```

A no-model replay on a copy of the retained TRL #6150 export removed its single
source-backed cache file and produced exactly the digest of the previously blocked
cache-cleaned export. The evidence reader can now read it; its substantive
correctness is still unvalidated. Automated tests cover distinct mechanical and
semantic allowances, missing-plan restoration, invalid mappings and policy-bound
admission. The first pilot remains 0/5; this policy has not yet established improved
live conversion yield.

### Planning reference handoff

The first conversion recovery cohort exposed a construction defect: accepted plans
used prose where contract requirement IDs and executable control names were needed.
The first two slots exhausted mechanical corrections without reaching semantic
review; the remaining work was stopped explicitly, preserving all five planned
slots and the original evidence. This does not establish source unsuitability.

New planning schemas require identifier-shaped requirement references, Python test
names and safe control names before accepting a design. Both author phases explain
that `requirement` references `contract.requirements[].id`, not `behavior`, and that
control arrays contain names rather than rationale. Validation errors list expected
and received references. Historical verification-plan parsing is unchanged; invalid
old planning caches fail closed rather than being silently rewritten. The frozen
recovery runtime remains unchanged; these corrections apply to subsequent runs.


### Planning noncompletion diagnostic

A subsequent PEFT #2962 diagnostic on the identifier-schema fix used all 20
planning turns for remote exploration and never called `submit_design`. It cost
$0.319434, produced no task, and reached neither semantic review nor solver trials.
It therefore did not exercise the repaired identifier handoff. Together with the
stopped recovery cohort's $4.110665, these attempts remain in the original ledger;
they are repeated-source diagnostics, not additional fresh-source yield.

Planning now reserves eight of its twenty model turns for synthesis. Exploration
is capped at $1.20 of the existing $2 allowance; synthesis receives only the
remaining shared-ledger allowance, including any outstanding reservations. Early
acceptance skips synthesis. Synthesis receives retained observations and schema
feedback, with further shell exploration unavailable. A shortened evidence
excerpt is marked explicitly, and full observations remain in the run directory.
`design-phases.json` records phase allocations, transitions, outcomes and costs. A saved design remains a structural milestone only:
independent review and executable evidence must still establish task validity.


The subsequent full conversion reached a complete semantic submission after an
additional mechanical issue: pytest's assertion-rewritten cache names include a
dotted pytest version, which the original importlib-based parser rejected. The
future sanitizer recognizes source-backed CPython cache names carrying released
pytest versions (for example `test_contract.cpython-312-pytest-8.4.2.pyc`). It still
rejects malformed names, orphan bytecode and links before deleting any files. Its
sanitation receipt uses policy version 2. The running frozen runtime was not
modified; its author removed the cache autonomously within the mechanical allowance.


The full synthesis diagnostic subsequently ended at its three-submission ceiling
for $4.061153, with no admitted task or isolated Harbor/solver trial. Independent
review caused real automatic improvements: fixed key/shape anchors, observed raw
compile prefixes, a consistent collection boundary, and same-depth leaf-collision
coverage. Its final rejection identified a deeper dotted-path shortcut. A separate
audit found that compiled fixtures still gave the configuration and checkpoint the
same target selection, leaving conditional checkpoint bypass indistinguishable.
Neither result establishes that the PR cannot become a useful environment.

Further experiments must preserve those outcomes and costs. A new experiment may
explicitly allocate more repair attempts within a declared monetary ceiling; it
must not rewrite the old allowance or count a repeated source as fresh-source yield.


### Input authority evidence in verifier review

Verifier policy 8 introduced a structured worksheet covering every public contract
requirement; current policy 10 retains that requirement. For applicable input interactions, it identifies the authoritative
input, a competing input/default/configuration, the relevant public condition,
a fixture where those inputs differ, the independently expected observation,
a conditional shortcut, and the mapped test that distinguishes it. Requirements
without such an interaction receive a grounded `not_applicable` explanation.
Only materially promised conditions belong in this inventory; it does not require
an arbitrary Cartesian product of all options.

Each row cites exact frozen source text and the public contract. The host resolves
exact quotes to line numbers and validates requirement/test references and cited
test or helper locations. Repeated public-contract text can use its first match;
repeated test text is resolved within the named test or a linked helper. Explicit
line numbers remain checked. Errors identify the affected row and candidate lines.
This checks evidence references, not semantic truth:
the judge must still determine whether the actual assertion distinguishes the
shortcut. A declared gap becomes required author feedback and cannot receive a
passing score. Existing records remain readable, but a previous-policy cache is
not reused as a current worksheet review. No reward function or overall validity
threshold changes. Improved live detection must be measured separately from
schema and reference validation.


A review-only replay of the retained third draft completed under policy 8 and
independently detected the previously missed compiled-path input substitution.
It cost $1.441381 and left the task unchanged. Its nine model responses included
three citation/test-reference corrections; policy 9 addresses that avoidable
formatting work without changing the detected semantic standard. This is static
review evidence, not an executed exploit or a task admission.

Policy 10 also accepts supplementary exact fixture quotes without requiring a
line number or treating each quote as an assertion. A distinguished row still
needs at least one assertion or call mapped to its test. Literal pytest fixture
aliases and same-module autouse fixtures, including their explicit dependencies,
are resolved; unrelated module fixtures cannot supply that evidence. These changes
remove reproduced reference-validation false rejections. They do not establish
that an assertion ran or proves the promised behavior. The live v25 conversion
retains its frozen policy 9 runtime.

### Isolated independent audits

The v25 PEFT diagnostic ended after two semantic submissions at $3.781021
because an independent audit imported a live probe and created Python bytecode
during reviewer reconsideration. All original files and the added cache are
preserved. This is audit-induced contamination, not a generator-quality outcome.

Use `isolated_audit_copy(source, new_audit_root, expected_digest=...)` from
`repo2rlenv.curation.audit_copy` for independent inspection. It verifies an exact
source digest, copies into a disjoint directory, and records source/copy file
inventories before and after the audit in `provenance.json`. Unexpected bytecode
or other changes fail the integrity check and remain available as evidence.
Never import a live task. For stdlib-only inspection subprocesses use Python
`-I -B` with `audit_subprocess_env()` and inspect the copy as data. This workflow
detects changes; it is not an OS sandbox or permission to execute task code.
Task execution and image builds still belong in remote environments.

### Required specification repairs

Specification preflight policy 2 separates required corrections from optional
polish. A required repair now prevents passing even with a score of 3 or 4, so
the campaign returns it to the author before cloud validation. Cosmetic changes,
extra repetition of established API semantics and unsupported concerns must not
be promoted into requirements. Historical `SpecificationReview` records retain
their previous semantics; current preflight uses `SpecificationPreflightReview`
and a new cache identity. Thresholds, read coverage and six-turn/$2 limits are
unchanged. This closes a feedback bypass observed in score-3 reviews containing
material repairs; it does not establish improved conversion yield. Frozen v26
continues with its original specification policy.
