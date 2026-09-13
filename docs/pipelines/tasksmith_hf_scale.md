# Tasksmith: HF bootstrap and generation campaign

This campaign starts from the supplied **114 PRs** across Transformers, Accelerate, TRL, Diffusers, PEFT and Tokenizers. It bootstraps all six repositories for CPU and GPU hosts before scaling task generation. Repository readiness is measured separately from per-PR task quality.

The bootstrap phase is complete: **6/6 CPU and 6/6 GPU-host checks passed** on September 13, 2026. Five repositories exercised CUDA operations on an NVIDIA L4; Tokenizers exercised its CPU implementation and a separate CUDA host check. All bootstrap workers are terminated. The conservative estimate across successful and failed bootstrap attempts is **$13.082577**, with no outstanding bootstrap reservations. [Exact bootstrap evidence](evidence/tasksmith-hf-bootstrap.json).

The initial source audit found 45 PRs compatible with the current modified-Python-source profile. This is a structural intake result, not 45 validated CPU tasks. Added/deleted source and Rust/CUDA changes remain in the inventory with explicit reasons. After retrying transient GitHub errors, 68 inputs have an explicit structural exclusion and one remains an intake retrieval error. The first generation panel contains ten selected PRs from that original pool.

The [intake inventory](evidence/tasksmith-hf-intake.json) preserves all 114 URLs, structural exclusions, and complete source pins/diff hashes for the 45 supported inputs. The CPU snapshot was restored successfully, and a separate import-origin audit confirmed that all six target packages load from their pinned source checkout inside `/workspace`.

The frozen panel produced **ten accepted new CPU Harbor tasks from its ten original PRs**: six Accelerate, three PEFT and one TRL. Eight final Sonnet rollouts succeeded; two failed with implementation errors that grading rejected. Every task has a failing baseline, passing actual-PR reference, rejected semantic mistakes, an accepted valid alternative and a reviewed Sonnet rollout. These tasks are separate from the earlier five-task pilot. A solver failure does not by itself make an environment invalid.

This was an iterative development campaign with orchestration fixes, verifier repairs and manually assembled evidence summaries. It does not establish first-attempt or universal PR conversion yield. The [final generation evidence](evidence/tasksmith-hf-generation.json) records source pins, bundle hashes, all 54 final trial receipts, artifact audits and stage costs.

```mermaid
flowchart TD
    MD["Original HF candidate file<br/>114 PR URLs"] --> AUDIT["Free metadata audit<br/>Pin refs and diff; retain unsupported inputs"]
    AUDIT --> PANEL["Frozen initial ten-PR panel"]
    AUDIT --> REPOS["Six pinned repository revisions"]
    REPOS --> CPU["Existing bootstrap on Modal VM<br/>CPU Docker build + offline behavioral smoke"]
    REPOS --> GPU["Native Modal image builder<br/>CUDA image + L4 behavioral smoke"]
    CPU --> CACHE["Docker filesystem snapshot<br/>Seven-day account-scoped cache"]
    CPU --> HINTS["Recorded dependency pins<br/>Target package excluded from hints"]
    GPU --> MATRIX["GPU readiness evidence<br/>Actual device and operations recorded"]
    PANEL --> TS["Tasksmith investigate → bootstrap → design → construct"]
    CACHE --> TS
    HINTS --> TS
    TS --> QC["Independent review / bounded repair<br/>Baseline, PR reference, wrong/valid controls, Sonnet"]
    QC --> TASK["Harbor task + exact evidence + cost"]
    TASK --> SCALE{"Budget and measured yield<br/>support another small panel?"}
    SCALE -->|Yes| PANEL
    SCALE -->|No| REPORT["Retain results and remaining budget"]
```

## Ten-task outcomes

| Original PR | Required tests | Task / verifier / leakage scores | Sonnet reward |
|---|---:|---|---:|
| [accelerate #3075](https://github.com/huggingface/accelerate/pull/3075) | 10 | 3 / 3 / 4 | 0 |
| [accelerate #3251](https://github.com/huggingface/accelerate/pull/3251) | 8 | 4 / 3 / 4 | 1 |
| [accelerate #3850](https://github.com/huggingface/accelerate/pull/3850) | 11 | 4 / 3 / 4 | 1 |
| [accelerate #3684](https://github.com/huggingface/accelerate/pull/3684) | 8 | 4 / 3 / 4 | 1 |
| [accelerate #3969](https://github.com/huggingface/accelerate/pull/3969) | 9 | 4 / 3 / 4 | 1 |
| [accelerate #3150](https://github.com/huggingface/accelerate/pull/3150) | 26 | 4 / 3 / 4 | 1 |
| [peft #2962](https://github.com/huggingface/peft/pull/2962) | 12 | 4 / 3 / 4 | 1 |
| [peft #3302](https://github.com/huggingface/peft/pull/3302) | 4 | 4 / 3 / 4 | 1 |
| [peft #3350](https://github.com/huggingface/peft/pull/3350) | 7 | 4 / 3 / 3 | 0 |
| [trl #6066](https://github.com/huggingface/trl/pull/6066) | 14 | 4 / 3 / 4 | 1 |

There are **54 final native trial records**: ten baselines, ten references, thirteen wrong implementations, eleven valid alternatives and ten Sonnet rollouts. Every baseline scores 0; every reference and valid alternative scores 1; every wrong implementation scores 0. Scores are model review assessments from 0 to 4. Tests are focused behavioral selections, not entire upstream suites.

The final delivery is `workspace/tasksmith-hf-scale/delivery.tar.gz` (97,672,840 bytes). It contains ten standalone task directories, an index, a manifest, review records, native trajectories/verifier logs/submitted source, original run summaries, bootstrap recipes and evidence. The committed evidence summary records its SHA-256. Full upstream source and traces stay in the delivery artifact rather than the Git diff.

The final artifact audit verified all ten task identities, all native result hashes, and unchanged learner starting source and actual-PR reference trees against the first construction artifacts. All ten copied bundles parse under Harbor 0.20.0; their learner source has no Git history. A literal scan of 14,307 packaged files found no configured credential values. This is an artifact integrity check, not an exhaustive security proof.

## What bootstrap proves

These small checks exercise real code without downloaded pretrained weights, datasets or model API calls. They are readiness checks, not complete upstream test suites or task verifiers.

| Repository | CPU check | GPU-host check |
|---|---|---|
| Transformers | Tiny BERT forward/backward with finite outputs/gradients | Same operations on CUDA |
| Accelerate | Device placement and optimizer step through `Accelerator` | Real CUDA placement and optimizer step |
| TRL | Log-probability and entropy equivalence plus autograd | Same numerical checks on CUDA |
| Diffusers | Tiny UNet forward/backward and DDPM scheduler step | Same operations on CUDA |
| PEFT | LoRA injection, adapter gradients and merge | Same operations on CUDA |
| Tokenizers | Compile Rust bindings, encode/decode and serialization | CPU tokenizer operations on a GPU host, plus a separate CUDA host check |

Tokenizers does not become GPU accelerated because a GPU is present. Similarly, a single L4 smoke test does not establish tensor parallelism, distributed training, FP8 or multi-GPU readiness.

CPU builds use the repository's existing `ensure_bootstrap` path with an explicit Dockerfile. They separately run the behavioral check under `docker --network none`, rather than trusting an image-build success as a smoke-test result. CUDA images are built remotely and tested in a native sandbox with network blocked. Modal VM Sandboxes cannot attach GPUs; using a native GPU sandbox is intentional. [Modal VM limitations](https://modal.com/docs/guide/vm-sandboxes).

The official PyTorch 2.11 CUDA runtime uses externally managed system Python. The GPU recipe creates a writable virtual environment with system packages visible, preserving its preinstalled CUDA PyTorch. Target dependencies install into that environment. The GPU configuration explicitly removes the bundled `spin` development tool from this disposable image: its Click upper bound conflicts with current Hub requirements. The full dependency check remains enabled.

## Commands and code

```bash
uv build --wheel
uv run repo2rlenv tasksmith bootstrap configs/tasksmith/hf-bootstrap.json \
  --resource cpu \
  --campaign workspace/my-existing-campaign \
  --output workspace/hf-bootstrap \
  --runtime-wheel dist/repo2rlenv-0.8.8-py3-none-any.whl \
  --env-file .env

uv run repo2rlenv tasksmith bootstrap configs/tasksmith/hf-gpu-bootstrap.json \
  --resource gpu \
  --campaign workspace/my-existing-campaign \
  --output workspace/hf-bootstrap \
  --runtime-wheel dist/repo2rlenv-0.8.8-py3-none-any.whl \
  --env-file .env
```

The command displays a compact readiness table; `--json` emits structured reports. A failed GPU attempt stops the batch before another allocation. Use `--resource both` when one matrix configuration is suitable for both sides. The shipped CPU and GPU matrices pin the same six repository revisions and record their distinct setup corrections through `extra_install`. Changed inputs require a new attempt directory. Failed and interrupted effects keep their receipts. Unknown sandbox creation is reconciled by its stable provider name before retrying.

Pass `--bootstrap-report workspace/hf-bootstrap/cpu/report.json` to import the recorded snapshot and dependency hints directly. The CLI checks source bindings, rejects conflicting explicit options and expired snapshots, and keeps your model and spending settings. The HF options preset allows a 160,000-character review context: the third task needed more targeted source evidence than the initial 100,000-character setting provided. This is a bounded maximum, and actual model usage remains metered. For manual configuration, the Tasksmith options file can include `worker_snapshot`, `worker_cpus`, `worker_memory_mb` and `bootstrap_hints`. Hints contain a source revision, base image, exact recorded dependency pins and their limited evidence scope. The investigator checks compatibility with each PR's own manifests; the per-PR bootstrap still runs its actual offline tests. Dependencies are warmed through the same source-free Docker prefix builder used by Tasksmith. Only compatible recipes can be cache hits.

```bash
uv run repo2rlenv tasksmith run configs/tasksmith/hf-first-ten.json \
  --options configs/tasksmith/hf-options.json \
  --bootstrap-report workspace/hf-bootstrap/cpu/report.json \
  --campaign workspace/my-existing-campaign \
  --output workspace/hf-first-ten \
  --runtime-wheel dist/repo2rlenv-0.8.8-py3-none-any.whl \
  --env-file .env
```

Snapshots are trusted builder caches containing repository images, not learner base images. Final Harbor bundles retain their ordinary portable Docker install recipe, private verifier and real PR reference. They do not require the private Modal snapshot or a worker-local image tag. The snapshot expires after seven days; readiness evidence and recipes remain. [Modal snapshot retention](https://modal.com/docs/guide/sandbox-snapshots).

Some PEFT and TRL revisions contain internal documentation symlinks, including `AGENTS.md`, `CLAUDE.md` and contribution guides. The investigator can select these explicitly with the Python profile's `materialize_document_links` list. After export, the snapshot builder resolves each selected link to an existing document inside the snapshot, copies its bytes into a regular file and records its target and SHA-256 in `snapshot-document-links.json`. It validates every selection before changing any file. Missing, cyclic, escaping, directory and source-code targets remain errors; unselected symlinks remain unsupported. `public_exclude` governs the later learner view and cannot repair a failed snapshot export.

## Separate repository readiness from task grading

A feature PR can add an API that its upstream test module imports during collection. Reversing the source patch then prevents the whole module from collecting, including unrelated tests. TRL #6066 demonstrated this: the original 14 selected upstream cases pass on the merged revision, but their module imports the newly introduced reward factory at its top level.

Tasksmith keeps those original tests for bootstrap readiness. At design time, `upstream_test_policy` defaults to `retain`: upstream selectors remain in grading, with any additional private tests appended. For a collection incompatibility, the designer can explicitly choose `replace`, explain why, and supply equivalent private behavioral tests. An empty replacement is rejected. Imports of new APIs belong inside test functions, so the starting code can collect the same cases and fail the feature assertions normally. Existing adjacent behavior must still pass. Both construction controls and the final Harbor verifier use the same grading selection; the original readiness profile stays unchanged. The chosen policy is recorded in task metadata.

```mermaid
flowchart TD
    HEAD["Pinned merged PR"] --> UPSTREAM["Run original upstream selectors offline"]
    UPSTREAM --> READY["Readiness evidence"]
    READY --> DESIGN["Design request and private verifier"]
    DESIGN --> RETAIN["Default: retain upstream selectors"]
    DESIGN --> REPLACE["Explicit replacement for collection incompatibility<br/>Port behavior; defer new API imports to test functions"]
    RETAIN --> SELECTION["One grading selection"]
    REPLACE --> SELECTION
    SELECTION --> CONTRAST["Identical test identities in both controls<br/>Feature fails before fix; adjacent behavior passes"]
    CONTRAST --> HARBOR["Export Harbor task and fixed PR reference"]
```

This does not turn a collection error into a successful baseline control. The construction contract still requires comparable test identities, real fail-to-pass behavior and adjacent pass-to-pass behavior.

| Responsibility | Owned implementation |
|---|---|
| Matrix schema, Dockerfiles, remote CPU build and dependency record | `src/repo2rlenv/tasksmith/bootstrap_matrix.py` |
| Actual CPU/GPU behavioral checks | `src/repo2rlenv/tasksmith/bootstrap_smoke.py` |
| Reservations, remote controller, snapshots and compute estimates | `src/repo2rlenv/tasksmith/matrix_runner.py` |
| Snapshot restoration | `src/repo2rlenv/execution/{base,modal}.py` |
| Hint validation, dependency warming and per-PR investigation | `src/repo2rlenv/tasksmith/{models,runner,worker}.py` |
| Exact investigation prompt | [Generated prompt reference](prompts/tasksmith.md#investigatemd) |
| Request design, verifier selection and exact author prompt | [Generated design prompt](prompts/tasksmith.md#designmd) |

## Budget and reporting

The original reproduction campaign had **$253.313684 available** at intake. It retains the same ledger and cap. Bootstrap CPU/GPU operations share a **$30 cap**. Generation began with a $140 run ceiling; the corrected six-candidate pass used a $120 ceiling, and the remaining four original PRs have an $80 ceiling. These are per-run limits, not additional campaign funding. Prior spending and unresolved reservations remain in the shared ledger before every new dispatch.

Model estimates come from recorded token usage. Compute estimates conservatively charge full allocations over the recorded interval, double that amount, and include an image-build allowance. Unknown model effects retain reservations. Workers are terminated and compute reservations reconciled after each run. These estimates are not provider invoices. [Modal rates](https://modal.com/pricing).

The author runtime allows up to 12,000 output tokens per model call, including reasoning and the complete structured artifact. A TRL design submission was truncated at the earlier 6,000-token ceiling. The larger allowance is a ceiling, not a request to fill it: the bridge still reserves every call against the same stage and campaign spending caps. Truncated responses remain failures and cannot commit incomplete artifacts or trigger partial tool effects.

For large repositories, the reviewer groups paths by directory and uses a bounded searchable catalogue. Every file remains addressable; an oversized read leaves the previous context intact. Literal searches also bound characters per match: a minified JSON trajectory can otherwise return its entire contents as one matching line. Truncated search excerpts identify the original line and columns and explicitly label omitted text.

These fixes came from completed tasks whose execution succeeded but whose final review could not read enough evidence. Saved bundles were reviewed again with their bound execution receipts. The sixth candidate completed before switching the remaining four original PRs to the corrected runtime; its worker was terminated, and the next inspection was interrupted before an author model call. Original attempts, reviews and the ten-PR selection remain intact.

A manual audit also found that two generated PEFT #2962 tests invented module paths that their fake model could not resolve. Those tests rejected an otherwise valid path-lookup strategy. The quality review prompt now explicitly checks whether test doubles preserve relevant real API invariants. A follow-up valid implementation failed those old tests, while a nested-module overmatching counterexample passed them. The repair replaced the mocks with real compiled modules and added independent path and shape expectations. Fresh controls now accept both valid implementations, reject both semantic mistakes, and a fresh Sonnet rollout succeeds. The earlier invalid verifier, failed rollouts and counterexamples remain in a separate evidence archive. The accepted revision is the corrected one; a model's usable label alone is not treated as proof when contradictory execution evidence exists.

PEFT #3302 needed stronger submodule checks: the initial generated assertion merely established that a model existed. The repair tests actual adapter weights, gradients and independently calculated forward outputs. All execution checks passed, but the first final review had omitted the middle of the recorded solver trace. A follow-up review reused the checksum-bound trial and received the complete recorded tool-call sequence plus the actual submitted-source diff. It did not pay for another successful rollout simply to compensate for a missing evidence excerpt.

PEFT #3350 exposed two further gaps. A second adapter could exist only in configuration without usable weights, and an implementation could handle only the outermost parameter wrapper. Successive repairs added independent numerical outputs, adapter switching and checkpoint reloads for multiple parameters on one module. Both wrong implementations are now rejected and the reference and valid alternative pass. Sonnet's attempted solution failed the standalone checkpoint-reload check; that is a useful solver failure on a valid task. Its recorded local checks focused on successful registration and loading, while grading checked the loaded numerical behavior.

TRL #6066 required two verifier repairs. All original examples used the same token budget, allowing an implementation to ignore the supplied budget. They also failed to distinguish mathematical verification from substring matching. The corrected tests vary the budget and reward bounds, check serialization with nondefault settings, and distinguish equivalent mathematical answers from misleading mentions of a gold answer. The original upstream tests still establish bootstrap readiness. Final validation uses a dedicated worker: a controller hold during an earlier shared-worker run was followed by a transport JSON error while collecting a probe result. That interruption is retained as execution evidence, not counted as a failed behavioral control.

For a resumed generation, `--generation-run PREVIOUS_RUN --reuse-evidence` can reuse unchanged baseline, reference and matching-model rollout results. The importer checks both the task identity and original result checksum; infrastructure failures execute again. Semantic probes run under the current review policy. No old reward is attributed to an edited task revision. Omit `--reuse-evidence` when fresh trials are wanted.

## Final cost and validation

This phase accounts for **$100.325379**, including all six repository bootstraps, generation attempts, reviews, repairs, rollouts and conservative compute estimates. All phase reservations are reconciled and all phase workers are terminated.

| Stage | Accounted estimate |
|---|---:|
| Repository investigation | $5.188584 |
| Task and verifier design | $2.256920 |
| Quality review | $52.739739 |
| Quality repairs | $6.750079 |
| Sonnet rollouts | $3.883004 |
| Remote compute, including CPU/GPU bootstrap | $29.507053 |
| **Total** | **$100.325379** |

The six-repository bootstrap accounts for $13.082577 of compute. Review was the largest model cost, partly because evidence had to be reviewed again after reader fixes. Unchanged completed executions were reused where their hashes matched. The last TRL review reused all prior controls and the successful rollout through a receipt-checking adapter, with no new remote execution; the recorded submitted-source diff showed that Sonnet implemented the function in a different public module and exported it correctly. This is useful evidence of implementation freedom.

Dividing the whole phase by ten gives about $10.03 per accepted task **including development retries and repository bootstrap**. Investigation plus initial/retried design totaled $7.445504 across the panel; that is not a quote for independently generating a production-quality task. Repeated-run costs and earlier model assessments are retained in the evidence summary rather than counted as additional environments.

The unchanged reproduction campaign now accounts for $309.038318 inside its $477.57 ledger cap, plus $22.43 of earlier spending outside that ledger. Historical unresolved reservations remain $15.543377, leaving **$152.988305 available**. These reservations were not erased. The requested 10–50 range is met at ten, with the remaining budget preserved.

The integration runtime passed **954 tests with three existing skips**, plus CI across Python 3.12/3.13/3.14, owned recipe contracts, real Pi/OpenCode SDK contracts against mock servers, lint and distribution builds. Generated prompt references match their sources, and MkDocs builds with existing historical-link warnings. [Generation-runtime CI](https://github.com/huggingface/Repo2RLEnv/actions/runs/34735190170).

The live campaign exercised Modal and Pi. Existing Daytona and OpenCode choices retain their shared contract coverage. GPU task generation, added/deleted source changes, Rust changes, multi-service environments and broad difficulty calibration remain outside this CPU panel.
