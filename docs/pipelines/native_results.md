---
title: "Native pipeline results"
---

The six original pipelines have **600 task entries in the recovered reference
inventories**: PR diff 181, PR runtime 100, commit runtime 100, code instruct 100,
equivalence tests 100 and CVE patches 19. The evidence comes from records dated
May–July 2026, inspected locally on **15 September 2026**. This page audits
historical results. It isn't a fresh recount of the Hub or a new execution
campaign.

The [dataset table](releases.md#native-pipelines) links every dataset and any
pinned manifest that exists. Counts leave out older revisions and duplicate
staging directories. They don't show that tasks are unique across pipelines.
Tasksmith and the research recipes have
[separate publication results](releases.md#tasksmith-and-research-recipes).

## What was checked

| Evidence | What it establishes | What it does not establish |
|---|---|---|
| Cached Hub manifest | Composition at a specific dataset revision | Current Hub contents or working oracles unless validation is recorded |
| Local publication staging | Retained task files, parsed metadata and source diversity | A fresh download or a successful run of every task |
| Generation-time checks | The generator recorded its acceptance conditions | Independent quality review or resistance to reward hacking |
| Oracle gate | The reference patch passed the recorded verifier | A sufficiently strong verifier or a solvable, unambiguous instruction |
| Solver sample | Outcomes for those task versions and attempts | A full-dataset solve rate or a controlled model comparison |

The old `validation_status = "verified"` field describes generation checks.
Don't translate it automatically into the newer
[evaluation label](task_evaluation_labels.md) of the same name.

## PR diff

The cached manifest lists **181 distinct task IDs across 26 repositories**. It
records composition only, with no per-task oracle results. The earlier
[100-task release report](../release_notes/v0.8.3/findings-pr_diff.md) describes
successful oracle trials and a 23-trial Sonnet 4.6 pilot with a median reward of
**0.71** and a range of **0.16–0.98**. That report doesn't validate all 181 later
entries.

The reward combines diff similarity with a semantic judge. Its continuous scores
aren't binary, test-passing solve rates. A complete generation denominator and
generation-cost ledger weren't recovered.

## PR runtime

The enriched manifest contains **100 tasks across 13 repositories**. Recounting
its individual validation rows confirms:

| Oracle outcome | Tasks |
|---|---:|
| Gold patch scores 1.0 and all tracked tests pass | 100 |
| Also no untracked failures and test command exits successfully | 88 |
| Also has a non-empty regression-test set (`eval_grade`) | 87 |

These are the historical verifier's criteria, not the newer independent quality
gate. The [release report](../release_notes/v0.8.3/findings-pr_runtime.md)
describes roughly **55–60%** solved in a Sonnet pilot of about 20 tasks. The
final raw sample wasn't recovered, so this is an observation from the report, not
a recomputed solve rate. Generation yield and total cost are unavailable.

## Commit runtime

The later manifest contains **100 tasks across 22 repositories**. Its task IDs
match the retained local export exactly, and all 100 carry generation-time
`validation_status = "verified"` metadata. The cached manifest was published
under the name `repo2rlenv-commit-runtime-v2`. It records composition, not a
full 100-task Harbor oracle-gate receipt.

The earlier [52-task cohort](../release_notes/v0.8.3/findings-commit_runtime.md)
has its own enriched manifest: **52 oracle/tracked passes, 47 clean-command
passes and 47 with a regression guard**. Those counts were recomputed from its
rows. This cohort predates instruction synthesis and isn't part of the 600-entry
inventory. Its top-level `pipeline` field wrongly says `pr_runtime`; its commit
URLs and release report identify it as commit runtime.

The development jobs that were kept include different task revisions,
incomplete records and agent-setup failures. They can't support a solve rate for
the final cohort, or the old guide's unqualified Opus claim. Full generation cost
is unavailable too.

## Code instruct

The local publication staging has **100 tasks**, 20 each from click, flask,
requests, attrs and starlette. Every required task artifact is present. The
complete generation log gives:

| Repository | Candidates | Exported | Yield |
|---|---:|---:|---:|
| pallets/click | 27 | 20 | 74.1% |
| pallets/flask | 28 | 20 | 71.4% |
| psf/requests | 24 | 20 | 83.3% |
| python-attrs/attrs | 23 | 20 | 87.0% |
| encode/starlette | 34 | 20 | 58.8% |
| **Total** | **136** | **100** | **73.5%** |

This corrects the earlier claim of **132 candidates / 75.8%**. A candidate is a
seed snippet, and model retries count within their candidate. The most common
rejections were oracles failing their own generated tests, and duplicate tasks.

The per-task `llm_cost_usd` field is **cumulative for the run**, not the cost of
that one task. Taking the final maximum once per repository gives **$3.775929 of
recorded synthesis cost**, or **$0.038 per export**. That includes retries up to
the last export, and excludes bootstrap, compute, solver rollouts and earlier
development. Summing all 100 task counters would overcount.

Five retained Sonnet 4.6 jobs, one task per repository, give **4 rewards of 1.0
and one reward of 0.0**, with no recorded trial errors. Their model cost totals
**$0.27053025** (about **$0.054 per trial**), not counting compute. That's a
small sample, not an 80% result across all 100 tasks. The earlier Codex “4/5”
claim was extrapolated after a repair, so it isn't included as a measured
result.

## Equivalence tests

The local publication staging has **100 tasks across seven utility-oriented
repositories**: sympy 30, toolz 20, boltons 15, more-itertools 13, pygments 12,
funcy 7 and hjson-py 3. Every required task artifact is present. This replaces
the old guide's “dataset pending” status.

Completed generation summaries account for **200 candidates and 100 exports**,
including runs for mpmath, setuptools and black that produced nothing. Other runs
have no final summary, so 200 is only a lower bound on the denominator. **Overall
yield is unavailable**; it isn't a measured 50%. Counting only the productive
repositories would inflate it further.

The cumulative counters from productive runs total **$2.512044**, or **at least
$0.025 per export**. That figure leaves out runs with no output, attempts after
the final export, bootstrap, compute and rollouts. It's a partial floor on the
accounting, not a production price.

| Model / agent | Sample | Observed outcome | Recorded model cost |
|---|---:|---|---:|
| Sonnet 4.6 / Claude Code | 5 unique tasks | 4 scored 1 after setup retries; 1 remained an installation error | $0.20258115 |
| GPT-5.3-Codex / Codex | 5 tasks | 5 scored 1 | $0.29694805 |
| Qwen3.6-35B-A3B / OpenHands SDK | 5 tasks | 5 scored 1 | Unavailable: recorder reports zero |

Sonnet's first job recorded one success and four setup errors. A retry job
recovered three of those four tasks, and the last failure was an agent installer
download error before solving began. Count the original five task identities
once, not as nine independent attempts. These models used **different task
samples**, so the table isn't a model leaderboard. Job totals may leave out
charges for failed setup retries, and compute isn't included. No separate
full-dataset oracle or quality gate was recovered.

## CVE patches

The cached composition manifest lists **19 tasks across six repositories**:
waitress 8, werkzeug 3, requests 3, mistune 2, flask 2 and sqlparse 1. It has no
per-task validation results or populated F2P/P2P counts. Earlier isolated smoke
runs don't establish a gate for this exact cohort.

The inventory is kept, with **verification evidence unavailable**. No solver
result matched to this cohort, full generation denominator or complete cost
ledger was recovered. The old blanket wording, “19 verified environments”,
claimed more than the evidence supports.

## Evidence and reproduction

The local search covered the original checkout's dataset staging areas,
generation logs, Harbor job and trial results, archived plans and release
findings, plus the Hugging Face download cache. Staged and flattened copies
weren't double-counted. The three complete local exports (commit runtime, code
instruct and equivalence tests) were checked for required artifacts and parsed
TOML metadata. This audit didn't build images, run models or spend cloud budget.

The `native_history` section of the
[portable measurement file](../data/pipelines.json) keeps source SHA-256 hashes,
pinned public manifest URLs, aggregate generation reports, per-repository
cumulative counters and sanitized solver outcomes. Local exports have
deterministic tree hashes. Raw transcripts, generated tasks and operational
scripts stay git-ignored, and the docs build doesn't need any of those private
local directories.

To refresh these results, first match task identities and versions, recount the
manifest rows, separate setup errors from verifier failures, and work out the
scope of every cost counter before you add it up. Then update the measurement
file and regenerate the [dataset](releases.md) and [cost](economics.md) tables as
described in [documentation maintenance](../contributing/DOCUMENTATION.md).
