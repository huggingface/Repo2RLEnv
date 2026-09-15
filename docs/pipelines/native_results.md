# Native pipeline results

The six original pipelines have **600 task entries in the recovered reference
inventories**: PR diff 181, PR runtime 100, commit runtime 100, code instruct 100,
equivalence tests 100 and CVE patches 19. The evidence comes from May–July 2026
records inspected locally on **15 September 2026**. This is a historical results
audit, not a fresh Hub recount or a new execution campaign.

The [dataset table](releases.md#native-pipelines) links every dataset and available
pinned manifest. Counts exclude older revisions and duplicate staging directories.
They do not establish that tasks are unique across pipelines. Tasksmith and the
research recipes have [separate publication results](releases.md#tasksmith-and-research-recipes).

## What was checked

| Evidence | What it establishes | What it does not establish |
|---|---|---|
| Cached Hub manifest | Composition at a specific dataset revision | Current Hub contents or working oracles unless validation is recorded |
| Local publication staging | Retained task files, parsed metadata and source diversity | A fresh download or a successful run of every task |
| Generation-time checks | The generator recorded its acceptance conditions | Independent quality review or resistance to reward hacking |
| Oracle gate | The reference patch passed the recorded verifier | A sufficiently strong verifier or a solvable, unambiguous instruction |
| Solver sample | Outcomes for those task versions and attempts | A full-dataset solve rate or a controlled model comparison |

The old `validation_status = "verified"` field describes generation checks. It
must not be translated automatically into the newer [evaluation label](task_evaluation_labels.md)
of the same name.

## PR diff

The cached manifest lists **181 distinct task IDs across 26 repositories**. It is
a composition manifest without per-task oracle results. The earlier
[100-task release report](../release_notes/v0.8.3/findings-pr_diff.md) describes
successful oracle trials and a 23-trial Sonnet 4.6 pilot with median reward **0.71**
and range **0.16–0.98**. That report does not validate all 181 later entries.

The reward combines diff similarity and a semantic judge. Its continuous scores
are not binary test-passing solve rates. A complete generation denominator and
generation-cost ledger were not recovered.

## PR runtime

The enriched manifest contains **100 tasks across 13 repositories**. Recounting
its individual validation rows confirms:

| Oracle outcome | Tasks |
|---|---:|
| Gold patch scores 1.0 and all tracked tests pass | 100 |
| Also no untracked failures and test command exits successfully | 88 |
| Also has a non-empty regression-test set (`eval_grade`) | 87 |

These are the historical verifier's criteria, not the newer independent quality
gate. The [release report](../release_notes/v0.8.3/findings-pr_runtime.md) describes
roughly **55–60%** solves in an approximately 20-task Sonnet pilot. The final raw
sample was not recovered, so this remains a report-level observation rather than
a recomputed solve rate. Generation yield and total cost remain unavailable.

## Commit runtime

The later manifest contains **100 tasks across 22 repositories**. Its task IDs
match the retained local export exactly; all 100 carry generation-time
`validation_status = "verified"` metadata. The cached manifest was published under
the `repo2rlenv-commit-runtime-v2` name. It contains composition, not a full
100-task Harbor oracle-gate receipt.

The earlier [52-task cohort](../release_notes/v0.8.3/findings-commit_runtime.md)
has a separate enriched manifest: **52 oracle/tracked passes, 47 clean-command
passes and 47 with a regression guard**. Those counts were recomputed from its
rows. This cohort predates instruction synthesis and is excluded from the
600-entry inventory. Its top-level `pipeline` field incorrectly says
`pr_runtime`; its commit URLs and release report identify it as commit runtime.

Retained development jobs include different task revisions, incomplete records
and agent-setup failures. They do not support a final-cohort solve rate or the
old guide's unqualified Opus claim. Full generation cost is also unavailable.

## Code instruct

The local publication staging contains **100 tasks**, with 20 each from click,
flask, requests, attrs and starlette. All required task artifacts are present.
The complete generation log gives:

| Repository | Candidates | Exported | Yield |
|---|---:|---:|---:|
| pallets/click | 27 | 20 | 74.1% |
| pallets/flask | 28 | 20 | 71.4% |
| psf/requests | 24 | 20 | 83.3% |
| python-attrs/attrs | 23 | 20 | 87.0% |
| encode/starlette | 34 | 20 | 58.8% |
| **Total** | **136** | **100** | **73.5%** |

This corrects the earlier **132 candidates / 75.8%** claim. Candidates are seed
snippets; model retries are included within a candidate. Common rejections were
oracles failing their generated tests and duplicate tasks.

The per-task `llm_cost_usd` field is **cumulative for the run**, not the cost of
that individual task. Taking the final maximum once per repository gives
**$3.775929 recorded synthesis cost**, or **$0.038 per export**. It includes retries
through the last export, and excludes bootstrap, compute, solver rollouts and
earlier development. Summing all 100 task counters would overcount.

Five retained Sonnet 4.6 jobs, one task per repository, give **4 rewards of 1.0
and one reward of 0.0**, with no recorded trial errors. Their model cost totals
**$0.27053025** (about **$0.054 per trial**), excluding compute. This is a small
sample, not an 80% result over all 100 tasks. The earlier Codex “4/5” claim was
an extrapolation after a repair, so it is not included as a measured result.

## Equivalence tests

The local publication staging contains **100 tasks across seven utility-oriented
repositories**: sympy 30, toolz 20, boltons 15, more-itertools 13, pygments 12,
funcy 7 and hjson-py 3. All required task artifacts are present. This supersedes
the old guide's “dataset pending” status.

Completed generation summaries account for **200 candidates and 100 exports**,
including zero-output runs for mpmath, setuptools and black. Other runs lack a
final summary, so 200 is a lower bound on the denominator: **overall yield is
unavailable**, not a measured 50%. Counting only productive repositories would
inflate it further.

Productive-run cumulative counters total **$2.512044**, or **at least $0.025 per
export**. Zero-output runs, attempts after the final export, bootstrap, compute
and rollouts are missing from that figure. It is a partial accounting floor,
not a production price.

| Model / agent | Sample | Observed outcome | Recorded model cost |
|---|---:|---|---:|
| Sonnet 4.6 / Claude Code | 5 unique tasks | 4 scored 1 after setup retries; 1 remained an installation error | $0.20258115 |
| GPT-5.3-Codex / Codex | 5 tasks | 5 scored 1 | $0.29694805 |
| Qwen3.6-35B-A3B / OpenHands SDK | 5 tasks | 5 scored 1 | Unavailable: recorder reports zero |

Sonnet's first job recorded one success and four setup errors. A retry job
recovered three of those four tasks; the final failure was an agent installer
download error before solving began. Count the original five task identities
once, not nine independent attempts. These models used **different task samples**,
so this table is not a model leaderboard. Job totals may omit charges for failed
setup retries; compute is not included. A separate full-dataset oracle or quality
gate was not recovered.

## CVE patches

The cached composition manifest lists **19 tasks across six repositories**:
waitress 8, werkzeug 3, requests 3, mistune 2, flask 2 and sqlparse 1. It does not
contain per-task validation results or populated F2P/P2P counts. Earlier isolated
smoke runs do not establish a gate for this exact cohort.

The inventory is retained, with **verification evidence unavailable**. No
cohort-matched solver result, full generation denominator or complete cost ledger
was recovered. The previous blanket “19 verified environments” wording was
stronger than the available evidence.

## Evidence and reproduction

The local search covered the original checkout's dataset stagings, generation
logs, Harbor job and trial results, archived plans and release findings, plus the
Hugging Face download cache. Staged and flattened copies were not double-counted.
The three complete local exports (commit runtime, code instruct and equivalence
tests) were checked for required artifacts and parsed TOML metadata. This audit
did not build images, run models or spend cloud budget.

The `native_history` section of the [portable measurement file](../data/pipelines.json)
retains source SHA-256 hashes, pinned public manifest URLs, aggregate generation
reports, per-repository cumulative counters and sanitized solver outcomes.
Local exports have deterministic tree hashes; raw transcripts, generated tasks
and operational scripts remain ignored. The docs build needs none of those
private local directories.

To refresh these results, match task identities and versions first, recount
manifest rows, distinguish setup errors from verifier failures, and establish
the scope of every cost counter before aggregating it. Update the measurement
file and regenerate the [dataset](releases.md) and [cost](economics.md) tables as
described in [documentation maintenance](../contributing/DOCUMENTATION.md).
