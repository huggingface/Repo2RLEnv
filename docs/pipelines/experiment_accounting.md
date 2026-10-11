---
title: "Experiment accounting: models, compute and stage costs"
navTitle: "Experiment accounting"
---

Audited **2026-09-30** from saved receipts and frozen reports. The [yield and cost overview](economics.md) gives the comparable scope definitions; this page explains what the experiments actually consumed. No paid reruns were needed for this audit.

## How the total is reconciled

**$1,586.71 accounted, plus $31.72 unresolved/reserved**, across the disjoint scopes below. The combined accounting exposure is $1,618.43. This is a recovered experiment subtotal, not an invoice or a complete lifetime project bill. Older native-pipeline costs and interactive assistant usage are outside it.

| Measurement scope | Accounted | Held separately | What it paid for |
|---|---:|---:|---|
| Initial research pilots + Tasksmith development/evaluation | $972.45 | $21.07 | 291 recipe exports + 50 final Tasksmith tasks; includes failures, shared work and $22.43 prior spend |
| Research-recipe expansion | $445.94 | $8.50 | 989 new exports across 14 recipes; earlier retained tasks excluded |
| CodeMidas campaign | $99.91 | $1.47 | 100 curated tasks; includes historical pilots, generation and evaluation |
| FrontierSmith campaign | $68.42 | $0.67 | 100 selected tasks; includes pilot, expansion and sample evaluation |

Do not add the Tasksmith expansion, original recipe pilots, or per-stage tables again: they are subsets of these rows. Parent-to-child budget transfers were excluded. The [accounting detail](experiment_accounting.md#how-the-total-is-reconciled) explains scope, attribution and evidence.

The initial program ledger contains **5,004 operations and $950.02 accounted**, plus $22.43 recorded as prior external spend. It includes the initial recipe pilots, Tasksmith, independent quality work and shared costs. The extra $22.43 has no recovered stage or provider split and is retained as an external prior amount.

Research expansion adds $445.94 in separate leaf ledgers. The repository controller also carries a $21.29 transfer for terminal finishing; the audit excluded that parent row and counted the child costs once. Operation IDs in those expansion ledgers were checked for overlap with the frozen initial ledger. CodeMidas and FrontierSmith have separate campaign ledgers; FrontierSmith's pilot is included exactly once.

This subtotal excludes unledgered interactive assistance, historical native pipeline expenses without a complete ledger, and any provider charge not represented by the selected records. Invoice-level reconciliation has not been performed. Do not divide it by the total published inventory: its scope includes development, discarded work, retained-task repair and different evaluation coverage.

### Initial program cost allocation

| Attributed population | Accounted | Held | Interpretation |
|---|---:|---:|---|
| Final 50 Tasksmith tasks | $649.56 | $9.53 | Directly attributable operations only |
| Other Tasksmith PRs | $56.02 | $0.79 | Attempts outside the final 50 |
| Recipe candidates | $58.51 | $8.00 | Initial generation/evaluation attributable to tasks |
| Shared or unattributed | $185.92 | $2.75 | Cannot safely allocate to a task or recipe |

The direct-allocation and stage views below partition the same $950.02 ledger. Neither includes the separate $22.43 prior amount.

| Initial program stage | Accounted | Held | Operations |
|---|---:|---:|---:|
| authoring and investigation | $99.57 | $2.32 | 2,211 |
| blind solver model | $36.41 | $12.00 | 176 |
| compute estimates | $413.15 | $3.00 | 593 |
| other unattributed | $142.16 | $2.75 | 1,142 |
| quality review and repair | $258.73 | $1.00 | 882 |

“Other/unattributed” includes recipe authoring and unclassified operations; it is not all compute. The initial ledger spans multiple experiments, so its compute/model ratio is not Tasksmith's isolated generation ratio.

## Tasksmith development and quality work

The archived inventory contains 56 distinct PRs and 50 final accepted tasks. Six other PRs and earlier prototypes remain outside that cohort. The archive does not establish a complete pre-screening denominator, so this is not a claim of 50/56 unattended yield. All 50 final blind solver configurations used `anthropic/claude-sonnet-4-6`; 19 reached full reward.

| Model role in initial program | Recorded model | Accounted | Held | Operations |
|---|---|---:|---:|---:|
| authoring and investigation | `anthropic/claude-sonnet-4-6` | $99.57 | $2.32 | 2211 |
| other unattributed | `anthropic/claude-opus-4-6` | $104.85 | $2.25 | 816 |
| other unattributed | `anthropic/claude-sonnet-4-6` | $19.29 | $0.00 | 301 |
| other unattributed | `openai/gpt-5-mini` | $0.02 | $0.50 | 6 |
| quality review and repair | `anthropic/claude-opus-4-6` | $65.00 | $0.00 | 245 |
| quality review and repair | `anthropic/claude-sonnet-4-6` | $52.51 | $0.00 | 323 |
| quality review and repair | `openai/gpt-6-astra` | $141.21 | $1.00 | 314 |

This model table covers explicitly model-labelled ledger rows. Blind Harbor trial rows are a separate stage in the program table and are not re-added here.

| Final task requirement | Tasks | CPU | Memory MiB | GPU |
|---|---:|---:|---:|---|
| CPU task | 39 | 1 | 2048 | None |
| GPU task | 1 | 2 | 8192 | 2 × L4 |
| GPU task | 6 | 4 | 16384 | 1 × L4 |
| GPU task | 4 | 4 | 16384 | 2 × L4 |

These are learner task requirements, not the controller sizes. Tasksmith used Modal CPU controllers and native Modal L4 execution for GPU tasks. Model inference was an API charge. The recovered cost scope does not provide a defensible per-task split of CPU, GPU, image build and shared idle costs.

| Recorded assistance in final 50 | Tasks |
|---|---:|
| design and bootstrap preparation | 4 |
| no confirmed task specific intervention | 15 |
| operational or review continuation | 1 |
| task or verifier edit | 28 |
| validation control repair | 2 |

Automated repair evidence exists for **35 tasks**. This overlaps the assistance categories; it is not 35 additional tasks. “No confirmed intervention” means history is incomplete, not proven hands-off generation. Per-task direct costs, resource requirements, solver rewards and assistance categories are retained in the [sanitized accounting data](../data/experiment-economics.json).

## Cloud resource measurements

Each row below counts saved worker records with their configured resources. Worker-hours include setup, builds, execution, waiting and idle time. Concurrent worker-hours add together; they are neither wall-clock completion time nor CPU utilization. Receipts with no stop timestamp are excluded from hours and exposed in the coverage count.

### Initial recipe/development workers

| Provider | CPUs | Memory MiB | Disk GB | Worker records | Worker-hours |
|---|---:|---:|---:|---:|---:|
| daytona | 2 | 4096 | 10 | 4 | 11.84 (3/4 timed) |
| daytona | 2 | 4096 | n/a | 1 | n/a (0/1 timed) |
| modal | 2 | 4096 | 10 | 4 | 7.45 (4/4 timed) |
| modal | 2 | 4096 | n/a | 1 | 0.93 (1/1 timed) |
| modal | 4 | 8192 | 10 | 11 | 22.51 (11/11 timed) |

### repository-expansion

| Provider | CPUs | Memory MiB | Disk GB | Worker records | Worker-hours |
|---|---:|---:|---:|---:|---:|
| daytona | 4 | 8192 | 10 | 26 | 28.23 (26/26 timed) |
| modal | 4 | 8192 | 10 | 6 | 2.27 (6/6 timed) |

### terminal-expansion

| Provider | CPUs | Memory MiB | Disk GB | Worker records | Worker-hours |
|---|---:|---:|---:|---:|---:|
| daytona | 4 | 8192 | 10 | 12 | 6.68 (12/12 timed) |

### swe-flow-expansion

| Provider | CPUs | Memory MiB | Disk GB | Worker records | Worker-hours |
|---|---:|---:|---:|---:|---:|
| daytona | 4 | 8192 | 10 | 18 | 2.59 (18/18 timed) |

### seta-seed2synth-expansion

| Provider | CPUs | Memory MiB | Disk GB | Worker records | Worker-hours |
|---|---:|---:|---:|---:|---:|
| daytona | 4 | 8192 | 10 | 10 | 8.73 (10/10 timed) |

### seta-evol-expansion

| Provider | CPUs | Memory MiB | Disk GB | Worker records | Worker-hours |
|---|---:|---:|---:|---:|---:|
| daytona | 4 | 8192 | 10 | 12 | 5.94 (12/12 timed) |

### tmax-expansion

| Provider | CPUs | Memory MiB | Disk GB | Worker records | Worker-hours |
|---|---:|---:|---:|---:|---:|
| daytona | 4 | 8192 | 10 | 11 | 11.09 (11/11 timed) |

### terminalworld-expansion

| Provider | CPUs | Memory MiB | Disk GB | Worker records | Worker-hours |
|---|---:|---:|---:|---:|---:|
| daytona | 4 | 8192 | 10 | 43 | 9.44 (43/43 timed) |

### dataarc-expansion

| Provider | CPUs | Memory MiB | Disk GB | Worker records | Worker-hours |
|---|---:|---:|---:|---:|---:|
| daytona | 4 | 8192 | 10 | 11 | 3.12 (11/11 timed) |

### CodeMidas

| Provider | CPUs | Memory MiB | Disk GB | Worker records | Worker-hours |
|---|---:|---:|---:|---:|---:|
| daytona | 4 | 8192 | 10 | 4 | 11.62 (4/4 timed) |
| daytona | 4 | 8192 | 30 | 1 | n/a (0/1 timed) |

### FrontierSmith, pilot + expansion

| Provider | CPUs | Memory MiB | Disk GB | Worker records | Worker-hours |
|---|---:|---:|---:|---:|---:|
| daytona | 2 | 4096 | 10 | 13 | 22.56 (13/13 timed) |

### How compute was priced

Historical recipe receipts generally used a 4-CPU / 8-GiB / 10-GB Daytona rate of **$0.00009215 per second ($0.33174/hour)** plus an explicit **$1 image-build allowance per worker**. The allowance is a conservative accounting choice, not a measured provider build charge. Short or failed workers can therefore have high cost per task. Modal estimates used their recorded resource/lifetime basis; they are not silently repriced with Daytona rates.

CodeMidas used the same nominal Daytona hourly rate with a **2× safety factor and $1 build allowance** on the shown cost receipt. FrontierSmith used 2-CPU / 4-GiB workers and lifecycle estimates without a free-tier deduction. These assumptions differ, so compute totals are not a hardware benchmark. Stored example cost receipts and their hashes are included in the accounting data; no claim is made about today's provider prices.

## Repository expansion control coverage

The completion audit matched executable bundle hashes to baseline/reference receipts for the new exports. Retained pilot tasks were outside this check. A missing match means evidence was not established by this audit, not that the task necessarily failed.

| Recipe | New exports | Matching baseline/reference evidence |
|---|---:|---:|
| r2e | 80 | 80 |
| r2e-gym | 80 | 80 |
| scaler | 80 | 0 |
| swe-gen | 80 | 80 |
| swe-next | 80 | 80 |
| swe-smith | 76 | 68 |

## Per-recipe generation detail

The initial pilot API costs below are already inside the initial program total. Expansion costs are disjoint from those pilot costs. Full-lifecycle compute cannot be allocated per recipe from the shared initial workers; no equal-share estimate is presented as measured spend. Stage buckets include retries. Exact historical prompt text and source behavior may differ from the current release.

### swe-smith

[swe-smith pipeline](repo_mutate.md). Earlier pilot: **24 exports, $0.65 generation API**, $0.00 held. Expansion: **76 new exports**, $2.41 API and n/a attributable compute. Final inventory: 100. Compute pool: `repository-expansion`.

Combined recorded pilot + expansion authoring API: **$3.05**, or **$0.031 per final task**. This excludes independent quality work and all compute.

Pilot author models: `anthropic/claude-opus-4-6` (1 calls), `anthropic/claude-sonnet-4-6` (27 calls).

| Expansion stage | Accounted API | Held | Operations |
|---|---:|---:|---:|
| Issue/instruction authoring | $2.41 | $0.00 | 119 |

| Expansion model | API cost | Calls with token receipts / settled calls | Input tokens | Output tokens |
|---|---:|---:|---:|---:|
| `anthropic/claude-sonnet-4-6` | $2.41 | 119 / 119 | 366,955 | 87,107 |

Earlier quality pilot: 5/5 baseline/reference control pairs passed; 5/5 original Sonnet attempts scored one; **5/5 selected after review/revision**. Those are different outcomes and apply only to the five-task pilot, not the expanded dataset.

Mutation and instruction generation are cheap on cached small repositories. Not every expansion export retained matching nop/reference receipts: 68 of 76 did in the completion audit. Five earlier quality-pilot tasks are not a gate for the final 100.

### r2e

[r2e pipeline](r2e.md). Earlier pilot: **20 exports, $1.46 generation API**, $0.00 held. Expansion: **80 new exports**, $17.73 API and n/a attributable compute. Final inventory: 100. Compute pool: `repository-expansion`.

Combined recorded pilot + expansion authoring API: **$19.19**, or **$0.192 per final task**. This excludes independent quality work and all compute.

Pilot author models: `anthropic/claude-sonnet-4-6` (47 calls).

| Expansion stage | Accounted API | Held | Operations |
|---|---:|---:|---:|
| Public specification | $2.13 | $0.00 | 80 |
| Equivalence tests and repairs | $15.60 | $1.50 | 287 |

| Expansion model | API cost | Calls with token receipts / settled calls | Input tokens | Output tokens |
|---|---:|---:|---:|---:|
| `anthropic/claude-sonnet-4-6` | $17.73 | 365 / 365 | 1,724,935 | 836,703 |

Earlier quality pilot: 5/5 baseline/reference control pairs passed; 5/5 original Sonnet attempts scored one; **5/5 selected after review/revision**. Those are different outcomes and apply only to the five-task pilot, not the expanded dataset.

Equivalence-test authoring and retries dominate API spend. Passing a frozen reference only establishes behavior on the generated tests; sampled solver success does not prove exhaustive equivalence.

### swe-gen

[swe-gen pipeline](pr_to_env.md). Earlier pilot: **20 exports, $0.38 generation API**, $0.00 held. Expansion: **80 new exports**, $1.63 API and n/a attributable compute. Final inventory: 100. Compute pool: `repository-expansion`.

Combined recorded pilot + expansion authoring API: **$2.01**, or **$0.020 per final task**. This excludes independent quality work and all compute.

Pilot author models: `anthropic/claude-sonnet-4-6` (20 calls).

| Expansion stage | Accounted API | Held | Operations |
|---|---:|---:|---:|
| Instruction authoring | $1.63 | $0.00 | 81 |

| Expansion model | API cost | Calls with token receipts / settled calls | Input tokens | Output tokens |
|---|---:|---:|---:|---:|
| `anthropic/claude-sonnet-4-6` | $1.63 | 81 / 81 | 280,792 | 52,817 |

Earlier quality pilot: 5/5 baseline/reference control pairs passed; 5/5 original Sonnet attempts scored one; **5/5 selected after review/revision**. Those are different outcomes and apply only to the five-task pilot, not the expanded dataset.

Reuses existing PR fixes and tests, so the measured API work is mostly instructions. Repository discovery, dependency setup and shared worker costs prevent treating the API figure as an all-in price.

### swe-next

[swe-next pipeline](swe_next.md). Earlier pilot: **20 exports, $1.22 generation API**, $0.00 held. Expansion: **80 new exports**, $7.51 API and n/a attributable compute. Final inventory: 100. Compute pool: `repository-expansion`.

Combined recorded pilot + expansion authoring API: **$8.73**, or **$0.087 per final task**. This excludes independent quality work and all compute.

Pilot author models: `anthropic/claude-sonnet-4-6` (20 calls).

| Expansion stage | Accounted API | Held | Operations |
|---|---:|---:|---:|
| PR task instructions | $7.51 | $0.00 | 85 |

| Expansion model | API cost | Calls with token receipts / settled calls | Input tokens | Output tokens |
|---|---:|---:|---:|---:|
| `anthropic/claude-sonnet-4-6` | $7.51 | 85 / 85 | 2,122,461 | 76,468 |

Earlier quality pilot: 5/5 baseline/reference control pairs passed; 4/5 original Sonnet attempts scored one; **5/5 selected after review/revision**. Those are different outcomes and apply only to the five-task pilot, not the expanded dataset.

Uses existing PR tests and fixes. Input context is large relative to instruction output; test discovery and offline setup can fail before export.

### r2e-gym

[r2e-gym pipeline](r2e_gym.md). Earlier pilot: **20 exports, $1.31 generation API**, $0.00 held. Expansion: **80 new exports**, $3.52 API and n/a attributable compute. Final inventory: 100. Compute pool: `repository-expansion`.

Combined recorded pilot + expansion authoring API: **$4.83**, or **$0.048 per final task**. This excludes independent quality work and all compute.

Pilot author models: `anthropic/claude-sonnet-4-6` (20 calls).

| Expansion stage | Accounted API | Held | Operations |
|---|---:|---:|---:|
| Commit task instructions | $3.52 | $0.75 | 81 |

| Expansion model | API cost | Calls with token receipts / settled calls | Input tokens | Output tokens |
|---|---:|---:|---:|---:|
| `anthropic/claude-sonnet-4-6` | $3.52 | 80 / 80 | 863,442 | 62,213 |

Earlier quality pilot: 5/5 baseline/reference control pairs passed; 5/5 original Sonnet attempts scored one; **5/5 selected after review/revision**. Those are different outcomes and apply only to the five-task pilot, not the expanded dataset.

Commit mining supplies the implementation and tests. The missing-response reservation is preserved; do not count an unknown response as free.

### scaler

[scaler pipeline](scaler.md). Earlier pilot: **20 exports, $0.00 generation API**, $0.00 held. Expansion: **80 new exports**, $0.00 API and n/a attributable compute. Final inventory: 100. Compute pool: `repository-expansion`.

Combined recorded pilot + expansion authoring API: **$0.00**, or **$0.000 per final task**. This excludes independent quality work and all compute.

Pilot author models: none; deterministic generation.

| Expansion stage | Accounted API | Held | Operations |
|---|---:|---:|---:|
| No authoring calls | $0.00 | $0.00 | 0 |

| Expansion model | API cost | Calls with token receipts / settled calls | Input tokens | Output tokens |
|---|---:|---:|---:|---:|
| None | $0.00 | 0 / 0 | 0 | 0 |

Earlier quality pilot: 5/5 baseline/reference control pairs passed; 4/5 original Sonnet attempts scored one; **4/5 selected after review/revision**. Those are different outcomes and apply only to the five-task pilot, not the expanded dataset.

Deterministic generators require no authoring LLM. Shared compute and later solver evaluation still cost money; construction format checks are not an independent semantic audit.

### endless-terminals

[endless-terminals pipeline](endless_terminals.md). Earlier pilot: **20 exports, $10.77 generation API**, $0.00 held. Expansion: **80 new exports**, $29.47 API and $12.05 attributable compute. Final inventory: 100. Compute pool: `terminal-expansion`.

Combined recorded pilot + expansion authoring API: **$40.23**, or **$0.402 per final task**. This excludes independent quality work and all compute.

Pilot author models: `anthropic/claude-opus-4-6` (105 calls).

| Expansion stage | Accounted API | Held | Operations |
|---|---:|---:|---:|
| Artifact/test building and repair | $18.26 | $0.00 | 162 |
| Design and suitability screening | $11.21 | $0.00 | 289 |

| Expansion model | API cost | Calls with token receipts / settled calls | Input tokens | Output tokens |
|---|---:|---:|---:|---:|
| `anthropic/claude-sonnet-4-6` | $29.47 | 451 / 451 | 2,588,243 | 1,446,891 |

Earlier quality pilot: 5/5 baseline/reference control pairs passed; 5/5 original Sonnet attempts scored one; **5/5 selected after review/revision**. Those are different outcomes and apply only to the five-task pilot, not the expanded dataset.

Terminal design and environment building include rejected attempts and repair. The initial pilot used Opus, the expansion Sonnet, so pooling them hides a model change.

### cli-gym

[cli-gym pipeline](env_repair.md). Earlier pilot: **20 exports, $22.99 generation API**, $1.00 held. Expansion: **5 new exports**, $3.54 API and $2.17 attributable compute. Final inventory: 25. Compute pool: `terminal-expansion`.

Combined recorded pilot + expansion authoring API: **$26.53**, or **$1.061 per final task**. This excludes independent quality work and all compute.

Pilot author models: `anthropic/claude-opus-4-6` (175 calls).

| Expansion stage | Accounted API | Held | Operations |
|---|---:|---:|---:|
| Environment break/repair proposals | $3.54 | $0.00 | 48 |

| Expansion model | API cost | Calls with token receipts / settled calls | Input tokens | Output tokens |
|---|---:|---:|---:|---:|
| `anthropic/claude-sonnet-4-6` | $3.54 | 48 / 48 | 874,797 | 61,281 |

Earlier quality pilot: 5/5 baseline/reference control pairs passed; 0/5 original Sonnet attempts scored one; **5/5 selected after review/revision**. Those are different outcomes and apply only to the five-task pilot, not the expanded dataset.

Only five expansion exports were measured, added to 20 retained pilot tasks. The first five sampled Sonnet rollouts scored zero before repairs; later repairs and reruns are separate evidence, not initial successes.

### swe-flow

[swe-flow pipeline](repo_reconstruct.md). Earlier pilot: **24 exports, $1.12 generation API**, $0.00 held. Expansion: **76 new exports**, $4.05 API and $18.86 attributable compute. Final inventory: 100. Compute pool: `swe-flow-expansion`.

Combined recorded pilot + expansion authoring API: **$5.17**, or **$0.052 per final task**. This excludes independent quality work and all compute.

Pilot author models: `anthropic/claude-sonnet-4-6` (48 calls).

| Expansion stage | Accounted API | Held | Operations |
|---|---:|---:|---:|
| Docstring reconstruction | $0.86 | $0.00 | 76 |
| Specification reconstruction | $3.19 | $0.00 | 76 |

| Expansion model | API cost | Calls with token receipts / settled calls | Input tokens | Output tokens |
|---|---:|---:|---:|---:|
| `anthropic/claude-sonnet-4-6` | $4.05 | 152 / 152 | 667,339 | 136,484 |

Earlier quality pilot: 5/5 baseline/reference control pairs passed; 4/5 original Sonnet attempts scored one; **3/5 selected after review/revision**. Those are different outcomes and apply only to the five-task pilot, not the expanded dataset.

Reuses source for reconstruction and generates docstrings/specifications. Compute dominates the expansion bill; two published instruction issues remain labelled needs_repair.

### seta-seed2synth

[seta-seed2synth pipeline](terminal_synth.md). Earlier pilot: **23 exports, $20.99 generation API**, $0.00 held. Expansion: **77 new exports**, $41.74 API and $12.90 attributable compute. Final inventory: 100. Compute pool: `seta-seed2synth-expansion`.

Combined recorded pilot + expansion authoring API: **$62.73**, or **$0.627 per final task**. This excludes independent quality work and all compute.

Pilot author models: `anthropic/claude-opus-4-6` (57 calls), `anthropic/claude-sonnet-4-6` (74 calls).

| Expansion stage | Accounted API | Held | Operations |
|---|---:|---:|---:|
| Artifact/test building and repair | $28.58 | $0.00 | 241 |
| Design and suitability screening | $6.83 | $0.00 | 112 |
| Infrastructure/contract review | $6.33 | $0.00 | 264 |

| Expansion model | API cost | Calls with token receipts / settled calls | Input tokens | Output tokens |
|---|---:|---:|---:|---:|
| `anthropic/claude-sonnet-4-6` | $41.74 | 617 / 617 | 5,478,964 | 1,686,904 |

Earlier quality pilot: 5/5 baseline/reference control pairs passed; 4/5 original Sonnet attempts scored one; **3/5 selected after review/revision**. Those are different outcomes and apply only to the five-task pilot, not the expanded dataset.

Design, build and review may repeat before export. Three of five earlier quality-pilot tasks were selected after revision; the published 100 are not all independently validated.

### seta-evol

[seta-evol pipeline](task_evolve.md). Earlier pilot: **20 exports, $9.77 generation API**, $0.00 held. Expansion: **80 new exports**, $30.75 API and $13.97 attributable compute. Final inventory: 100. Compute pool: `seta-evol-expansion`.

Combined recorded pilot + expansion authoring API: **$40.52**, or **$0.405 per final task**. This excludes independent quality work and all compute.

Pilot author models: `anthropic/claude-opus-4-6` (25 calls), `anthropic/claude-sonnet-4-6` (43 calls).

| Expansion stage | Accounted API | Held | Operations |
|---|---:|---:|---:|
| Artifact/test building and repair | $20.46 | $1.25 | 154 |
| Design and suitability screening | $6.55 | $0.00 | 92 |
| Infrastructure/contract review | $3.75 | $0.00 | 146 |

| Expansion model | API cost | Calls with token receipts / settled calls | Input tokens | Output tokens |
|---|---:|---:|---:|---:|
| `anthropic/claude-sonnet-4-6` | $30.75 | 391 / 391 | 3,825,329 | 1,284,989 |

Earlier quality pilot: 5/5 baseline/reference control pairs passed; 5/5 original Sonnet attempts scored one; **3/5 selected after review/revision**. Those are different outcomes and apply only to the five-task pilot, not the expanded dataset.

Evolution can produce ambiguous instructions or weak state checks. One candidate was unfinished and one model charge remained unresolved at stop.

### tmax

[tmax pipeline](tmax.md). Earlier pilot: **20 exports, $22.54 generation API**, $0.00 held. Expansion: **35 new exports**, $58.83 API and $14.68 attributable compute. Final inventory: 55. Compute pool: `tmax-expansion`.

Combined recorded pilot + expansion authoring API: **$81.37**, or **$1.479 per final task**. This excludes independent quality work and all compute.

Pilot author models: `anthropic/claude-opus-4-6` (150 calls).

| Expansion stage | Accounted API | Held | Operations |
|---|---:|---:|---:|
| Artifact/test building and repair | $38.22 | $5.00 | 241 |
| Design and suitability screening | $13.99 | $0.00 | 309 |
| Infrastructure/contract review | $6.62 | $0.00 | 214 |

| Expansion model | API cost | Calls with token receipts / settled calls | Input tokens | Output tokens |
|---|---:|---:|---:|---:|
| `anthropic/claude-sonnet-4-6` | $58.53 | 733 / 733 | 6,698,349 | 2,562,447 |
| `openai/gpt-5.4-mini` | $0.30 | 27 / 27 | 146,473 | 49,404 |

Earlier quality pilot: 5/5 baseline/reference control pairs passed; 4/5 original Sonnet attempts scored one; **1/5 selected after review/revision**. Those are different outcomes and apply only to the five-task pilot, not the expanded dataset.

Multi-pass design/build/review makes failed attempts expensive. The final target was reduced to 55; eight expansion candidates were unfinished. A small unsuccessful GPT-5.4-mini comparison is included in cost.

### terminalworld

[terminalworld pipeline](terminalworld.md). Earlier pilot: **20 exports, $12.31 generation API**, $1.25 held. Expansion: **80 new exports**, $54.34 API and $46.13 attributable compute. Final inventory: 100. Compute pool: `terminalworld-expansion`.

Combined recorded pilot + expansion authoring API: **$66.66**, or **$0.667 per final task**. This excludes independent quality work and all compute.

Pilot author models: `anthropic/claude-opus-4-6` (239 calls).

| Expansion stage | Accounted API | Held | Operations |
|---|---:|---:|---:|
| Artifact/test building and repair | $20.60 | $0.00 | 498 |
| Design and suitability screening | $29.92 | $0.00 | 1679 |
| Infrastructure/contract review | $3.81 | $0.00 | 203 |
| Additional tests | $0.02 | $0.00 | 1 |

| Expansion model | API cost | Calls with token receipts / settled calls | Input tokens | Output tokens |
|---|---:|---:|---:|---:|
| `anthropic/claude-sonnet-4-6` | $54.34 | 2381 / 2381 | 12,131,699 | 1,196,456 |

Earlier quality pilot: 5/5 baseline/reference control pairs passed; 4/5 original Sonnet attempts scored one; **1/5 selected after review/revision**. Those are different outcomes and apply only to the five-task pilot, not the expanded dataset.

Recorded sessions often fail suitability screening: 1,145 of 1,293 candidates were screened out. That explains much of the 6.2% raw yield. Three published verifier gaps remain labelled needs_repair.

### dataarc

[dataarc pipeline](dataarc.md). Earlier pilot: **20 exports, $18.55 generation API**, $0.00 held. Expansion: **80 new exports**, $14.53 API and $12.04 attributable compute. Final inventory: 100. Compute pool: `dataarc-expansion`.

Combined recorded pilot + expansion authoring API: **$33.08**, or **$0.331 per final task**. This excludes independent quality work and all compute.

Pilot author models: `anthropic/claude-opus-4-6` (63 calls).

| Expansion stage | Accounted API | Held | Operations |
|---|---:|---:|---:|
| Artifact/test building and repair | $11.80 | $0.00 | 113 |
| Infrastructure/contract review | $2.73 | $0.00 | 111 |

| Expansion model | API cost | Calls with token receipts / settled calls | Input tokens | Output tokens |
|---|---:|---:|---:|---:|
| `anthropic/claude-sonnet-4-6` | $14.53 | 224 / 224 | 1,607,392 | 647,246 |

Earlier quality pilot: 5/5 baseline/reference control pairs passed; 5/5 original Sonnet attempts scored one; **0/5 selected after review/revision**. Those are different outcomes and apply only to the five-task pilot, not the expanded dataset.

High construction yield does not establish task quality. The published cohort used recipe v1; current v2 differs. None of five earlier quality-pilot tasks was selected under that pilot review despite five initial solver rewards of one.

## CodeMidas stage accounting

GPT-6 Luna handled source/task construction and ordinary solving; GPT-6 Sol handled construction review, independent rollout review and difficulty screening. Daytona workers ran CPU tasks. Costs include 233 construction attempts with historical revisions, while the current-collection yield uses 213 attempts and 128 unique exports. Reporting the narrower yield does not remove historical costs.

| Stage | Accounted | Held | Ledger operations |
|---|---:|---:|---:|
| adversarial attempt | $0.39 | $0.00 | 8 |
| compute estimate | $11.71 | $0.00 | 5 |
| construction design | $2.58 | $0.00 | 1229 |
| construction review | $28.32 | $0.89 | 386 |
| construction tests | $4.14 | $0.03 | 1367 |
| frontier screen | $23.14 | $0.00 | 428 |
| ordinary solver | $2.13 | $0.00 | 546 |
| other | $0.00 | $0.00 | 5 |
| rollout review | $27.49 | $0.55 | 1368 |

The adversarial-attempt spend does not mean those checks succeeded: all 100 curated tasks remain blocked for full-method acceptance. Review found concrete defects in 26/128 exports. [CodeMidas results](codemidas.md#measured-local-campaign) give the solver and source breakdown. Token totals are not reconciled across every author and agent trace here, so no whole-campaign token figure is claimed.

## FrontierSmith stage accounting

All authoring, seed creation, reviews and sampled rollouts used `gpt-6-sol` in separate contexts. The 100-task selection cost includes the original ten-task pilot, original seed authoring, 153 candidate attempts, two post-construction generator repairs and collection review. It is not the cost of a single successful generation run.

| Model stage | API cost |
|---|---:|
| baseline and sampled programs | $25.52 |
| blind rollouts | $2.86 |
| collection contract review | $1.58 |
| collection diversity review | $0.51 |
| formulation and review | $5.70 |
| infrastructure and bounded repair | $24.10 |
| other development calls | $0.56 |
| post construction repair | $0.15 |
| seed authoring | $0.54 |
| semantic diversity review | $3.14 |

Add $3.76 estimated compute to $64.66 API usage for $68.42 accounted; keep $0.67 held separately. Baseline/sample generation and test infrastructure dominate the cost. All 100 are construction-checked; 24 final tasks had blind rollout attempts and 21 completed feasibly. None has full optimization-aware quality acceptance. References are sampled feasible programs, not proven optima. [Collection details](frontiersmith.md#measured-100-task-collection) retain those limits. A unified token total across author and Harbor-agent receipts is not claimed.

## Historical native pipeline accounting

The native datasets predate the campaign ledgers above. A fresh read of archived task metadata recovered the generation model for three 100-task cohorts. This names the recorded model; it does not recover missing token, retry or compute charges.

| Native pipeline | Cohort metadata | Recorded synthesis API total |
|---|---|---:|
| commit_runtime | `anthropic/claude-sonnet-4-6` on 100 tasks | n/a |
| code_instruct | `anthropic/claude-sonnet-4-6` on 100 tasks | $3.775929 |
| equivalence_tests | `anthropic/claude-sonnet-4-6` on 100 tasks | $2.512044 |

Code-instruct's total sums the last run-cumulative counter for each of five repositories. Equivalence tests include only productive-run counters, so its total is a lower bound. Commit-runtime has no complete synthesis ledger despite its model stamp. These partial amounts are excluded from the program subtotal to preserve its stated scope.

PR-diff, PR-runtime and CVE-patch costs and generation-model coverage remain unavailable for their exact selected inventories. Do not infer an old run's model from today's example configuration. Historical solver samples include Sonnet 4.6, GPT-5.3-Codex and Qwen3.6-35B-A3B, on different tasks; their small model-cost samples and missing setup/compute charges are documented in [native results](native_results.md).

## Evidence and refresh

The [portable audit data](../data/experiment-economics.json) records SHA-256 fingerprints of the saved reports, ledgers and worker receipts, grouped costs and selected sanitized task metadata. Paths are relative evidence identifiers, not required checkout files. The [pipeline summary](../data/pipelines.json) supplies the published sample definitions and native history; the [FrontierSmith report](../data/frontiersmith-campaign.json) supplies the final collection identity.

To refresh: identify the exact output cohort; freeze the source reports; exclude transfer envelopes and duplicate/revision rows; retain unknown-call holds; reconcile each API stage and resource group; then update the portable summaries. Do not sum run-cumulative per-task counters or multiply an old unit cost by a newer dataset size.

The generator validates totals, per-model/stage sums, token coverage, source references and transfer exclusions before writing pages. A clean clone can rebuild the documentation using only the committed summaries:

```bash
python3 docs/_tools/generate_metrics.py
python3 docs/_tools/generate_metrics.py --check
```

Token counts are reported input/output counts on recovered settled receipts, not a new price calculation. Cache fields are retained separately and must not be added twice. Uncertain requests have no usable token totals. The recipe expansion receipts used recorded LiteLLM estimates; other campaigns used their configured rate tables or agent-reported costs. None is presented as an invoice.
