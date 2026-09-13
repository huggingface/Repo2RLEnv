# Tasksmith: HF bootstrap and generation campaign

This campaign starts from the supplied **114 PRs** across Transformers, Accelerate, TRL, Diffusers, PEFT and Tokenizers. It bootstraps all six repositories for CPU and GPU hosts before scaling task generation. Repository readiness is measured separately from per-PR task quality.

The bootstrap phase is complete: **6/6 CPU and 6/6 GPU-host checks passed** on September 13, 2026. Five repositories exercised CUDA operations on an NVIDIA L4; Tokenizers exercised its CPU implementation and a separate CUDA host check. All bootstrap workers are terminated. The conservative estimate across successful and failed bootstrap attempts is **$13.082577**, with no outstanding bootstrap reservations. [Exact bootstrap evidence](evidence/tasksmith-hf-bootstrap.json).

The initial source audit found 45 PRs compatible with the current modified-Python-source profile. This is a structural intake result, not 45 validated CPU tasks. Added/deleted source and Rust/CUDA changes remain in the inventory with explicit reasons. After retrying transient GitHub errors, 68 inputs have an explicit structural exclusion and one remains an intake retrieval error. The first generation panel contains ten selected PRs from that original pool.

The [intake inventory](evidence/tasksmith-hf-intake.json) preserves all 114 URLs, structural exclusions, and complete source pins/diff hashes for the 45 supported inputs. The CPU snapshot was restored successfully, and a separate import-origin audit confirmed that all six target packages load from their pinned source checkout inside `/workspace`.

The first ten-PR panel currently has **eight accepted new CPU tasks**: Accelerate #3075, #3251, #3684, #3850, #3969 and #3150, plus PEFT #2962 and #3302. PEFT #3350 and TRL #6066 are still in progress. These tasks are separate from the earlier five-task pilot. Every accepted task has a failing baseline, passing actual-PR reference, rejected semantic mistakes, an accepted valid alternative and a reviewed Sonnet rollout. Seven final Sonnet rollouts succeeded; the remaining one failed with an ordinary implementation error that the verifier rejected. A solver failure does not by itself make an environment invalid.

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

For a resumed generation, `--generation-run PREVIOUS_RUN --reuse-evidence` can reuse unchanged baseline, reference and matching-model rollout results. The importer checks both the task identity and original result checksum; infrastructure failures execute again. Semantic probes run under the current review policy. No old reward is attributed to an edited task revision. Omit `--reuse-evidence` when fresh trials are wanted.

The campaign report will distinguish repository smoke readiness, generated bundles and tasks usable under `practical-generation-v1`; count failed attempts; record cache restoration/hits; and show actual measured stage costs. The target is 10–50 environments subject to those results and the unchanged budget, not a promise to spend the full allowance to reach fifty.
