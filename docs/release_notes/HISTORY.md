# Version history

Current release highlights followed by historical notes. `CLAUDE.md` carries
only the compressed summary; the detail lives here.

For per-release deep dives see the sibling pages (`v0.8.2.post3.md`,
`v0.8.3/`).

## v0.9.1 — Windows CLI startup and release checks

Released September 15, 2026.

Fixes the Windows CLI crash introduced in 0.9.0: even `--version` and `--help`
previously failed while importing the POSIX-only `fcntl` module. A shared
standard-library lock now uses `flock` on POSIX and byte-range locking on
Windows, preserving exclusive controller ownership and the blocking history
checkout lock. Thanks to KNambiarDJsc for the report and initial fix
([#128](https://github.com/huggingface/Repo2RLEnv/issues/128),
[#129](https://github.com/huggingface/Repo2RLEnv/pull/129)).

CI and publication now require fresh Windows wheel checks on Python 3.12–3.14:
CLI startup, recipe discovery, native UTF-8 task emission/static validation and
real process-lock contention. Full native Windows Tasksmith, research-recipe
and quality-controller execution remains unsupported; use Linux, macOS or WSL.
The remaining artifact-permission and process-cleanup work is tracked in
[#130](https://github.com/huggingface/Repo2RLEnv/issues/130).

This release also includes the Python, documentation/build-tool and Pi/OpenCode
runtime dependency updates merged after 0.9.0. Upgrade with
`pip install --upgrade --upgrade-strategy eager repo2rlenv==0.9.1`, including any
extras your pipeline needs. The Windows CLI/locking fix does not require changes
to existing generated datasets. Versioned examples use the 0.9.1 runtime wheel.

## v0.9.0 — Tasksmith and owned generation recipes

Released September 15, 2026.

Tasksmith and 14 research-inspired generation recipes now ship alongside the
six native pipelines. Tasksmith uses LangGraph with Pi or OpenCode to investigate
a merged PR, bootstrap its repository, design a task and private verifier, and
run bounded review and repair. CPU execution supports Daytona and Modal; the
implemented GPU route uses Modal L4 GPUs. Tasksmith and the recipes remain
experimental; their current scope is documented in the [pipeline guide](../pipelines/README.md).

The owned recipes cover repository mutation, PR and commit mining, function
reconstruction, terminal synthesis, task evolution, environment repair and
reasoning instances. CLI discovery exposes each method's options and source
provenance. Campaigns record budgets, resumable work and execution evidence;
the shared quality loop reviews Harbor bundles and learner traces and writes
explicit evaluation labels. Generation alone does not establish task quality.

The README now provides a shorter quickstart and complete route table. Detailed
guides document stage diagrams, exact prompts, dataset validation scope and
measured yield and costs. Versioned examples point to the 0.9.0 runtime wheel.

Python 3.12–3.14 remain supported. To upgrade an existing environment, use
`pip install --upgrade --upgrade-strategy eager repo2rlenv==0.9.0`, adding any
extras your pipeline needs.

Dependency maintenance updates the locked HTTP clients and LiteLLM to patched
versions, with security minimums for direct dependencies and transitive lock
constraints. Existing pip environments should upgrade transitive dependencies
as well; uv constraints are not included in wheel metadata. Dependabot now tracks
the Python and coding-agent locks alongside GitHub Actions.

Native generation preserves parametrized pytest IDs containing spaces in both
test discovery and the copied runtime verifier. GitLab tasks use the source host
for clone URLs and commit references. Hub publishing accepts both flat datasets
and `tasks/<id>` layouts, and Windows generation uses portable paths and explicit
UTF-8 output.

`validate --deep` checks task assets and metadata without running a sandbox.
`--oracle` also checks reference-solution assets; named recipes can provide a
solve script without a patch. These checks do not establish oracle success or
task quality. Use Harbor execution and the quality workflow for that evidence.

Generated datasets retain the code they were emitted with. Updating the package
does not replace existing `tests/verifier.py` files or repair saved F2P/P2P lists.
For an affected dataset, recover complete test IDs from the original test results,
update the verifier, and rerun baseline and oracle checks before publishing a new
revision. Metadata-only reference corrections do not change rewards; clone-URL
corrections need a new image build. Preserve earlier dataset revisions and keep
validation claims tied to the revision actually tested.

`generate` and `bootstrap` now accept `--llm-endpoint` and `--llm-key-env`.
Keyless self-hosted providers can use LiteLLM's native credential handling.

Existing configurations with a custom `llm.endpoint` must explicitly name
`llm.api_key_env` to send a hosted provider's default key there. For example,
an authenticated OpenAI-compatible gateway that previously used `OPENAI_API_KEY`
implicitly now needs `--llm-key-env OPENAI_API_KEY` or
`llm.api_key_env: OPENAI_API_KEY`. The CLI warns when that default key is set
but withheld. An explicitly named, unset key variable now raises an error
instead of falling back to a different key. Default hosted endpoints are unchanged.

---

## v0.1.0 — first release

`pr_diff` (originally `pr_mining_lite`) + HF Hub publish + diff-similarity
reward.

## v0.2 — bootstrap

Merged into `main` (not separately released): bootstrap phase, Rich UI module,
cost tracking, content-addressed cache keyed on bootstrap options.

## v0.3.0 — sandbox verification

`pr_runtime` (sandbox-verified PR mining) + auto-trigger bootstrap from
`generate` + structural quality filters + targeted test invocation + CI/CD
(ruff + matrix tests + release workflow).

## v0.4.0 — polyglot + Harbor compliance

Polyglot log parsers (Go / Cargo / Jest) + Harbor end-to-end compliance fixes:
`task.name` format, `solve.sh` shim, `/logs/verifier/reward.txt`, PATH prelude
for non-Python toolchains, defensive git install.

## v0.5.0 — commit-level mining

`pr_stream` (continuous PR mining with watermark state) + `commit_runtime`
(commit-level mining, SWE-GEN style). Both Harbor-verified.

`pr_stream` was **removed in v0.8.3** as scope-creep — `pr_runtime` handles the
same niche on its own.

## v0.6.0 — first LLM-synthesized pipelines

`mutation_bugs` (procedural AST bug injection, inspired by SWE-smith) +
`code_instruct` (repo-anchored OSS-Instruct with executable verifiers, inspired
by Magicoder). Both Harbor-verified on `pallets/click` (mean reward 1.000).

## v0.7.0 — function-level synthesis + CVEs

`equivalence_tests` (R2E-style function-level synthesis — extract a real
function, LLM writes an equivalence test against a `reference_<name>` oracle,
gold patch fills the candidate) + `cve_patches` (OSV-driven CVE → fix-commit
pipeline, reuses the `pr_runtime` validation harness). Both Harbor-verified.

## v0.8.0 — refactor mining

`refactor_synthesis` (Python-native rename-refactor mining — drops the
v1.0-planned JVM RefactoringMiner dependency; commit-message regex + diff
verification + multi-criteria structural+behavioral verifier). Harbor-verified
on `pallets/click` (mean reward 1.000).

## v0.8.3 — pipeline audit

**Removed `mutation_bugs` + `refactor_synthesis`.** Both were binary-reward,
Python-only, and the lowest-signal pipelines in the set: synthetic AST bugs are
unrealistic, and renames are a near-no-op RL target. `pr_stream` removed in the
same pass.

Shared helpers (`make_unified_diff`, `build_binary_eval_script`) moved from
`mutation_bugs.py` into `pipelines/_eval_script.py`.

## v0.8.4 — input sources + GitLab

Input-source abstraction (GitHub / GitLab / local, capability-gated) + GitLab MR
mining for `pr_diff` / `pr_runtime` (#62) + **`commit_runtime` promoted to
stable**.

`commit_runtime` gained LLM-synthesized leak-free instructions
(`synthesize_with_llm`, default on) + a `max_pass_to_pass` cap. Audit went
33% → 100% clean; Opus solves the sampled tasks (4/4 genuine). Reference
dataset: `…commit-runtime-test` (100 envs).

## Anti-contamination pass (PR #69)

Sandbox-verified tasks were gameable — an agent could fetch the published fix
(and the hidden tests) for the repo it was asked to fix. Now baked into the
emitter for **every** task via `pipelines/_env_guard.py`:

- **git-history scrub** — strip the repo to `base_commit`: remove `origin`,
  prune future refs/commits, gc.
- **egress guard** — `environment/docker-compose.yaml` blackholes PyPI + GitHub
  hosts so `pip download` / `git fetch` / web-fetch fail. Model API and agent
  install stay up.

Also in this pass:

- `cve_patches` ships a **graded F2P/P2P verifier** (was binary whole-suite,
  which scored the gold patch 0.0 on unrelated suite failures).
- A **leak-stripped instruction** for `cve_patches`.
- **Agentic PoC-test synthesis** (`_poc_agent.py`) — an LLM with shell access in
  the vulnerable sandbox writes a regression test for no-test CVEs.
- `bootstrap` re-bootstraps when a cache hit points at an evicted image.

Reference dataset `…-cve-patches` (19 verified envs).

**The principle: the environment enforces, the prompt never asks.**

## v0.8.6 — `code_instruct` self-improvement

The v0.6 prompt explicitly *forbade* using repo APIs, producing generic
Codeforces-lite tasks (baseline audit: mean repo-anchoring 1.4/5; zero of 20
tasks imported the target package).

Reoriented around genuine repo anchoring with four post-synthesis gates in
`_oss_instruct.py` — `check_repo_anchoring`, `check_symbol_collision`,
`check_test_strength`, `task_fingerprints` — plus `max_attempts_per_seed`
retries (default 1 → 3). Post-fix audit: RA = 4.95, TR = 4.95, zero scores ≤ 2.

Multi-agent validation (claude-code + Sonnet 4.6, codex + GPT-5.3-Codex,
openhands-sdk + Qwen3.6-35B via HF Router) surfaced a second bug: all three
models correctly implemented the requested logic but wrote to natural filenames
(`ranged_float.py`, `fetcher.py`), failing pytest `from task_module import ...`
collection with `ModuleNotFoundError`. Fix: append a `task_module.py`
delivery-contract paragraph to the emitted `instruction.md`. Solve rate
40% → 80% at fixed dataset size.

Reference dataset `…-code-instruct` (100 envs across 5 Python repos) — the first
published dataset for this pipeline. `code_instruct` stays **experimental**
pending graded reward + polyglot support.

## v0.8.7 — `equivalence_tests` self-improvement

The v0.7 pipeline had a full-source-in-instruction leak **and** a 97% Stage-B
failure rate on click. Root cause: extracted functions referenced repo-internal
types (`Argument`, `FC`) that don't exist in the standalone `task_module.py`, so
the import crashed before pytest reached the LLM's assertions.

Landed: leak-free instruction (`signature_only_source`), annotation-strip at
bake time (`strip_annotations`), scope-aware purity + self-containment filter in
the extractor (`_references_only_safe_names`), `is_module_importable` pre-flight,
recursion-safe rename (`rename_function_ast`), feedback-driven retry
(`max_attempts_per_function` default 1 → 3 — documented but not previously
wired), test-strength gate (`check_equivalence_test_strength`), task dedup
(`_equivalence_fingerprint`), a sharper prompt, `.debug_skips` dumps, and
`all_tests_passed` moved to the shared `_eval_script.py`.

**Reference dataset deferred.** The 5-repo click/flask/requests/attrs/starlette
survey yielded only ~8 pure candidates combined — framework-heavy repos are
structurally weak fits for equivalence testing. v0.8.7 is pipeline-hardening
only; the dataset ships once we survey utility-heavy libs (packaging,
itsdangerous, markupsafe, dateutil, …). `equivalence_tests` stays
**experimental**.

## v0.8.8 — docs site

Version bump + docs site published, Google Search Console verification, README
pointed at the live site, `scripts/` untracked.

## In progress — `pr_to_env` (RFC 0007)

Designed, not yet on `main`. The **import-shape** sibling of `pr_runtime`:
consumes an explicit list of curated PR URLs rather than mining a repo's
history. One URL → one Harbor task, or fail closed with a per-URL reason. Same
task shape, same graded F2P/P2P verifier, same anti-contamination guards; the
reused machinery is imported verbatim from `pr_runtime.py`.

Ships with a 12-gate quality layer (M3) landing across milestones M1–M4; the
gate list lives in RFC 0007.

## Planned

- `env_setup` (RFC 0008) — Repo2Run / SetupBench-style: the agent makes a bare
  repo's tests run green.
- `test_synthesis` (RFC 0009) — SWE-Flow-style TDD.
- `issue_runtime` (RFC 0010).
- Graded rewards for the binary synthesis pipelines.
- LLM-judged QA gate.
