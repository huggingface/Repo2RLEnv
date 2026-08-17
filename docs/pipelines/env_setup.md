# `env_setup`

Repo2Run/SetupBench-style: hand an agent a **bare, un-bootstrapped repo** and grade it on making the repo's own test suite install and run green — from nothing. No dependencies, no venv, no toolchain beyond the bare language base image. The reward is the fraction of the repo's own tests that pass under whatever install recipe the agent produces.

`env_setup` turns our existing `bootstrap/` machinery — today a build-time step every sandboxed pipeline consumes silently — into a first-class training and eval target. Bootstrap itself is an agent-with-shell loop (an LLM iterates `apt install` / `pip install -e .` / `pytest --collect-only` until the smoke gate passes); this pipeline exposes that same loop as the task an agent-under-eval solves.

**As of v0.9 the reward is graded, not binary**: the emitted `tests/verifier.py` (the same `_pr_runtime_verifier.py` `pr_runtime` ships, byte-for-byte, unmodified) scores `reward = f2p_rate × p2p_rate` to `/logs/verifier/reward.txt`. `env_setup`'s P2P list is always `[]` — there is no pre-existing passing set to protect, the agent starts from zero — and `grade()` maps an empty P2P to `p2p_rate = 1.0`, so `reward = f2p_rate` falls out with **no verifier change**. `resolved` is the strict SWE-bench bool (all F2P pass); the oracle recipe scores `1.0` by construction, proved before the task ships (see "The gold recipe" below).

| | |
|---|---|
| Status | **experimental** (RFC [0008](../rfcs/0008-env-setup.md)) |
| Sandbox required at gen | Yes — Docker via the [bootstrap phase](../reference/BOOTSTRAP.md), **plus** a second container that proves the distilled recipe |
| LLM required at gen | Yes — bootstrap's agent loop, **plus** 1–3 recipe-distillation calls per `(repo, ref)`. `--llm` is required. |
| LLM required at run | No — the verifier is pure stdlib |
| Source | GitHub · GitLab only — `required_capabilities = {Capability.REMOTE_CLONE}`. A local `file://` repo is rejected at pre-flight: the emitted Dockerfile clones the repo itself, and a `docker build` can't reach a host path. |
| Languages | Polyglot from v1 — Python, Node, Go, Rust, Java, C/C++ (auto-detected by bootstrap) |
| Inspiration | [Repo2Run](https://github.com/bytedance/Repo2Run) (arXiv:2502.13681), [SetupBench](https://github.com/microsoft/SetupBench) (arXiv:2507.09063), [EnvBench](https://github.com/JetBrains-Research/EnvBench) (arXiv:2503.14443) |

## What we produce per repo

`env_setup` emits **one task per `(repo, ref)`**, named by resolved SHA (not the ref string — `HEAD` isn't a stable id):

```
<owner>__<repo>-envsetup-<resolved_sha[:12]>/
├── task.toml
├── instruction.md
├── environment/
│   └── Dockerfile          # bare base + repo@ref. NO installs.
├── tests/
│   ├── test.sh             # gate 0 → gate ½ → gate 1 → verifier.py
│   ├── verifier.py         # _pr_runtime_verifier.py, unmodified
│   ├── env_prelude.sh      # venv activation / exports, sourced by both gates
│   ├── f2p.json            # target test ids
│   ├── p2p.json            # [] — always empty
│   ├── provenance.json     # {probe, base_commit, language, package, dist_name}
│   ├── provenance_read.py  # one-shot reader: emits probe + base_commit + language
│   ├── provenance_run.sh   # dispatches to the right probe under the prelude
│   ├── provenance.py       # Python probe (shipped only when language == python)
│   ├── provenance.js       # Node probe (shipped only when language == node)
│   └── test_roots.json     # pathspecs gate ½ cleans of untracked files
└── solution/               # omitted entirely when emit_solution=False
    ├── patch.diff          # a diff that CREATES /workspace/setup.sh
    └── solve.sh             # git apply patch.diff && bash /workspace/setup.sh
```

`environment/Dockerfile` follows `pr_diff`'s build-time-clone pattern (`ARG GIT_TOKEN=` build-arg, an authenticated clone URL, then `git remote set-url origin <clean url>` so no credential persists in a layer) with four deliberate differences from every other sandboxed pipeline:

1. `FROM` is the **bare** language base — `base_image_for(language)` from `bootstrap/language.py`, the same one bootstrap started from. Not a "close enough" reconstruction: generation-time verification builds this exact Dockerfile.
2. **No dependency installation whatsoever.** The toolchain layer installs only `git`, `ca-certificates`, `curl`, and `python3` (the last purely so `tests/verifier.py` can run on non-Python bases). Installing the repo's own dependencies here would delete the task — this is the one mistake that silently voids the pipeline.
3. **No sentinel injection, no baseline commit.** An earlier draft committed a marker attribute into the package root so it would survive git operations; PEP 610 metadata (see the provenance gate, below) replaces that outright.
4. `git reset --hard <base_commit>` + `git clean -fdx` already leave a clean tree at the resolved SHA, so that SHA is the tamper-restore anchor directly — no separate commit to keep in sync.

`instruction.md` is templated (no LLM call). It states the repo, the pinned commit, the goal ("make the project's own test suite build and run from this bare starting point"), the time budget, and that installing system/language packages is expected. It does **not** name the target tests, the package manager, or hint at the recipe — with one disclosed exception, covered below.

### `[metadata.repo2env.env_setup]`

| Key | Type | Meaning |
|---|---|---|
| `language` | str | `LanguageHint` value from bootstrap detection |
| `base_image` | str | The bare `FROM` |
| `test_cmd` | str | The normalized command `test.sh` runs (joined `test_cmds`) |
| `runner` | str | `pytest` \| `go` \| `cargo` \| `jest` — resolved at generation time and baked into `test.sh --runner` |
| `target_test_count` | int | `len(f2p)` |
| `recipe_lines` | int | Non-comment lines in the gold `setup.sh` — a crude difficulty proxy |
| `recipe_attempts` | int | Distillation attempts needed (1 = clean first pass) |
| `oracle_setup_time_sec` | float | Wall-clock for the gold `setup.sh` |
| `oracle_test_time_sec` | float | Wall-clock for the suite run after setup — the evidence `verifier_timeout_sec` gets tuned on |
| `agent_time_budget_sec` | int | `max_setup_time_sec`, mirrored for dataset-card queries |
| `base_commit` | str | Resolved SHA; the anchor the tamper restore uses |
| `has_lockfile` | bool | A `uv.lock` / `poetry.lock` / `package-lock.json` / `Cargo.lock` / `go.sum` exists at `ref` — a repo with one is substantially easier |
| `bootstrap_cost_usd` | float | LLM spend for this ref's bootstrap + distillation |
| `provenance_gate` | bool | Was the option on |
| `provenance_probe` | str | What actually shipped: `direct_url` \| `path` \| `none` |
| `reward_granularity` | str | `"graded"` — this is what records the property `reward_kinds` itself doesn't encode |
| `oracle` | str | `"recipe"` \| `"none"` (when `emit_solution=False`) |

The F2P list lives in `tests/f2p.json` (where the verifier reads it), not duplicated into the TOML — `target_test_count` is the queryable summary.

## How generation works

For each candidate `(repo, ref)` (`repo.ref` is always first; `refs` in options adds more, each paying a **full bootstrap**):

1. **`ensure_bootstrap()`** — same as every sandbox-required pipeline. Gated on **`verify_passed`**, not just `smoke_passed`: `smoke_passed` only checks the live agent shell, `verify_passed` replays the same commands in a *fresh* `docker run --rm` from the committed image, which catches state the agent set that `docker commit` dropped. A bootstrap from `--user-dockerfile` is rejected by name (`bootstrap_source_unsupported`) — it reports `language=UNKNOWN`, and there's no bare base to distil against.
2. **Normalize `test_cmds`**, drop blanks. An empty result skips (`no_runnable_test_cmds`).
3. **Confirm the base image agrees.** `bare_base_image(spec, lang)` (the same expression bootstrap's `runner.py` evaluated) is compared against the `FROM` line bootstrap reconstructed. A mismatch skips the ref (`base_image_mismatch`) rather than emitting a task whose recipe was distilled against one base and would replay against another.
4. **Build the task Dockerfile** — bare base + `git clone` @ resolved SHA, no installs (see above).
5. **Distill the recipe** (below) inside a container built from *that exact Dockerfile*.
6. **Run `bash setup.sh && test_cmds`** with xtrace disabled around the capture (a `+ cmd` trace line interleaved into a jest log corrupts every subsequent test id — this pipeline captures identically at generation and at run time). Red → retry with the failure fed back (capped at `max_recipe_attempts`, default 3, **total**). Green but the tracked tree is dirty → same retry loop, different complaint ("do not modify files tracked in the repository").
7. **Resolve the runner** (`detect_runner`) on the same joined command string the verifier will see. `unknown` skips the ref (`runner_undetectable`) rather than baking a value the verifier can't use.
8. **Parse the green log** for the target test set. `SKIPPED` tests are dropped explicitly (an agent that installs nothing still "passes" a skipped test). Apply `min_target_tests` first, *then* `max_target_tests` — the floor asks "is this repo gradeable," the cap asks "how much do we grade," and truncating first would let the cap silently answer the floor's question.
9. **F′ — dry-run the whole emitted `tests/test.sh`** (gate 0 → gate ½ → gate 1) inside that same known-good container. On a gate-0 (provenance) failure, degrade the probe to `none` and re-run — there is no intermediate rung. On a gate-½/gate-1 failure, skip (`gates_unverified`): those gates have no weaker form worth shipping. **This is where "the oracle scores 1.0" is actually established** — it runs even when `oracle_gate=False`, because a gate that can't pass on a known-good environment would zero every agent.
10. **Emit the task.** If `emit_solution and oracle_gate`, run `harbor run -a oracle` as a final shipping gate; anything short of `1.0` drops the task rather than shipping a broken oracle.

See [RFC 0008](../rfcs/0008-env-setup.md) for the full flowchart and the failure-mode table.

## The gold recipe

Neither bootstrap artifact is directly usable as an oracle: `dockerfile_reconstruction` replays *every* action from the transcript (failed attempts, `grep`s, `pytest` invocations included) and isn't reliably reproducible; `rebuild_cmds` assumes installation already happened. So `_setup_recipe.py` **distills, then proves**:

- **Source, in preference order:** the transcript's `BASH` turns → `dockerfile_reconstruction` + `rebuild_cmds` (cold path, for cache entries that predate transcript linking) → skip (`no_recipe_source`).
- **One LLM call per attempt** (1–3 total). The prompt asks for a single deterministic `set -euo pipefail` script, successful-path-only, no test invocation, no `pip install`/`npm install` of the repo's own released package, and — same reason as that last rule — **no modification of tracked files**.
- **Verification:** build the Dockerfile the task will actually ship, run `bash setup.sh && test_cmds` under `recipe_verify_timeout_sec`. Green *and* `git diff --quiet HEAD --` clean → accepted as the oracle. Red, or green-but-dirty → feed the last ~4 KB of output back and ask for a corrected script.
- **Exhausted** → skip (`recipe_unverified` or `recipe_edits_tracked_files`), dump every attempt to `<out_dir>/.debug_skips/<task_id>/` (the convention `equivalence_tests` established in v0.8.7).

`emit_solution=False` does **not** skip distillation — the only route to the F2P set *is* distill → build → run green → parse, so killing distillation would mean no F2P set and nothing to emit. The flag governs emission, not generation: distillation, the green run, and F′ all run as normal; the pipeline just doesn't write `solution/`, `oracle_gate` is forced off (there's no `solve.sh` for `harbor run -a oracle` to execute — F′ is the proof instead), and metadata records `oracle="none"`. Useful for a held-out eval split where the recipe shouldn't exist in the artifact at all.

## Verification (`tests/test.sh`) — three gates ahead of the shared verifier

`tests/test.sh` mirrors `pr_runtime`'s eval-script head (PATH prelude, `SCRIPT_DIR`, `/logs/verifier` setup) plus a sourced `env_prelude.sh` (the venv-activation / export fragments recovered from *this repo's* `test_cmds` — shipped as a file and sourced, never `eval`'d, because a quoted value like `export PYTEST_ADDOPTS='-p no:randomly'` would break string interpolation). Everything downstream runs under both preludes.

### Gate 0 — provenance

With egress **open** (see below), an agent can run `pip install click`, get the *released* package into site-packages, and watch the repo's own tests pass green against it — without ever making `/workspace` installable. Gate 0 is the one thing standing in front of that shortcut:

- **Python** (`probe = direct_url`): PEP 610's `direct_url.json` (installers MUST write it for a local/VCS/URL install, MUST NOT for an index install) **OR** the import resolving to a path under `/workspace` outside `site-packages`/`dist-packages`. This replaces an earlier sentinel-attribute design outright — PEP 610 metadata isn't forgeable by a `grep` + two-line append the way a committed marker was.
- **Node** (`probe = path`): `require.resolve` → `realpathSync`, passing iff the resolved path is under `/workspace` and **not** under any `node_modules` segment (the `node_modules` exclusion is the whole probe — `npm i <released-pkg>` lands under `/workspace/node_modules`, which a naive "under `/workspace`" check would pass).
- **Go, Rust** (`probe = none`): `go test ./...` / `cargo test` compile `/workspace` by construction — the substitute-the-released-package shortcut doesn't exist there.
- **Java, C/C++** (`probe = none`): no probe we'd trust yet. Recording `none` is more honest than shipping a gate that checks nothing.

One read of `provenance.json` (via `provenance_read.py`), and it **fails closed** — a missing `python3` or unreadable JSON scores `0.0` (`provenance_unreadable`), never a silent pass. Degradation is a single step, the language's probe → `none`, taken only when F′ watches the probe fail on the known-good oracle container. There's no intermediate rung: `path` is a strict subset of `direct_url` (import-location-only vs. dist-metadata-**or**-import-location), so it can never pass where `direct_url` failed.

### Gate ½ — restore the graded tests

```bash
git -C /workspace checkout "$R2E_BASE_COMMIT" -- .
git -C /workspace clean -fdq -- "${R2E_ROOTS[@]}"   # test_roots.json, always non-empty
```

Restores **every** tracked file (not a computed path list derived from the F2P ids — that fails the whole `git checkout` on one non-matching pathspec, and Go/Rust F2P ids carry no file to compute a list from at all), then `git clean`s a fixed set of test/config pathspecs (`tests/`, `conftest.py`, `pytest.ini`, `jest.config.*`, a recursive `**/conftest.py`, with `.venv/`, `node_modules/`, `**/site-packages/` excluded so the agent's own install survives) to catch anything the agent *added* — a restore of tracked files can't delete a new file. This is what stops an agent from rewriting a graded test body to `assert True`.

### Gate 1 — the graded reward

```bash
set +x
( <test_cmds> ) > /logs/verifier/test_output.log 2>&1
TEST_EXIT_CODE=$?
set -x
python3 tests/verifier.py --log ... --f2p tests/f2p.json --p2p tests/p2p.json \
  --runner '<resolved runner>' --test-cmds '<normalized cmds>' \
  --exit-code "$TEST_EXIT_CODE" --out-dir /logs/verifier
```

`--runner` is passed **explicitly**, resolved once at generation time (step 7 above) rather than re-detected at run time — an empty or malformed run-time command string would resolve to `unknown` and silently turn the graded reward into a binary one. `reward = f2p_rate` (P2P is always empty, so `p2p_rate = 1.0` and the multiplication is a no-op); `resolved` requires every F2P test to pass. Half-solving scores half — this pipeline explicitly rejects exit-code-only grading.

## Anti-contamination posture

`env_setup` is the **only pipeline in the set that ships egress open**, and that's inherent to the task shape, not an oversight:

| Guard | State | Why |
|---|---|---|
| Egress guard | **Off, and not an option** | `pip`/`apt`/`cargo` installs from a live index **are** the task. Neither `_env_guard.egress_guard_compose` (v1, used by `pr_runtime`/`commit_runtime`/`cve_patches`) nor `egress_firewall_compose` (v2, used by `pr_to_env`) is called, and there's no option to turn one on. |
| Git-history scrub | **Off by default** (`scrub_git_history=False`) | The repo's own `CONTRIBUTING.md`, `.github/workflows/ci.yml`, and commit history are legitimate solve context for a setup task, not a leak. Available via the option for parity with the rest of the set. |
| Provenance gate | **On by default** | The one real hole (gate 0, above). |
| Tamper restore | **On, always** | The F2P set is baked, so the graded test bodies must be too (gate ½). |

Every other sandboxed pipeline in this repo (`pr_runtime`, `commit_runtime`, `cve_patches`) blocks egress precisely so an agent can't fetch the published fix for the bug it's solving. That defense doesn't transfer here: the task *is* "reach the package index and figure out what to install," so blocking it would make the task unsolvable, not safer. **The provenance gate buys back the enforcement the network guard would otherwise provide** — it doesn't stop an agent from downloading packages (that's the point), it stops an agent from downloading the *one specific package* that would let it skip making `/workspace` itself installable.

**The tamper restore is the one guard this pipeline discloses in the prompt**, and it's a deliberate exception to *the environment enforces, the prompt never asks*. `instruction.md` carries exactly one sentence about it:

> Your solution must not depend on modifications to files tracked in the repository; the repository's tracked files are restored before grading.

The distinction that makes this an exception worth making, rather than a hole in the principle: gate ½ restoring the whole tracked tree isn't negotiable (the pytest-config surface — `addopts`, `collect_ignore` — is exactly where grading gets gamed, and Go/Rust F2P ids carry no file to narrow the restore to). Real setup work sometimes legitimately edits tracked files (pin a dep, drop a `use_2to3`), and the subset whose effect is read *at test time* would silently score 0 with no way for the agent to know why. Withholding that fact wouldn't buy a more uncontaminated eval — it would buy an unfair one and a confusing transcript. An agent told the rule can satisfy it (install from the tree as it is) without learning anything about the F2P set, the probe, or the reward shape.

## The SWE-bench-parity loss

Every other pipeline in this repo ships `solution/patch.diff` as a source-code diff that a harness can apply directly to score the oracle. `env_setup` breaks that pattern on purpose: `patch.diff` is a unified diff that **creates** `/workspace/setup.sh` — applying it alone leaves the script sitting on disk, unexecuted, and scores **0**. The recipe is an artifact to *run*, not an edit to apply. `solution/solve.sh` is the actual executable oracle:

```bash
#!/bin/bash
set -euxo pipefail
cd /workspace
git apply --verbose --reject "$(dirname "$0")/patch.diff"
bash /workspace/setup.sh
```

`harbor run -a oracle` uses `solve.sh`, not `patch.diff` — this is what forced `HarborTask.solve_script` to become a real field (previously `solve.sh` was hardcoded to `git apply patch.diff`). **Consumers that ingest only `solution/patch.diff` get the full recipe text — useful as an SFT target — but not a self-applying fix.** If you're building a dataset loader that assumes "apply `patch.diff`, verify, done" (the SWE-bench-style convention every other pipeline here follows), it will not work for `env_setup` tasks; you need to run `solve.sh` (or replicate its two lines) instead.

## Prerequisite: bootstrap

Same as every sandbox-required pipeline — `cmd_generate` triggers `ensure_bootstrap()` automatically. The difference here is that the resulting **image is discarded**; only the transcript (or the reconstruction, on the cold cache-hit path), `test_cmds`, and `language` survive into the recipe distillation step. See [`reference/BOOTSTRAP.md`](../reference/BOOTSTRAP.md).

## Options

```python
class EnvSetupOptions(_BaseOptions):
    # --- Discovery ---
    limit: int = 20
    refs: list[str] | None = None            # commits/tags to base tasks on; None ⇒ HEAD only

    # --- Signal floors ---
    min_target_tests: int = 5                # reject suites too small to grade meaningfully
    max_target_tests: int = 0                # cap the F2P set; 0 ⇒ whole suite
    max_setup_time_sec: int = 1800           # agent budget → task.toml agent.timeout_sec

    # --- Oracle recipe ---
    emit_solution: bool = True               # False ⇒ eval-only split; distillation still runs
    max_recipe_attempts: int = 3             # TOTAL attempts, not retries-after-the-first
    recipe_verify_timeout_sec: int = 1800
    llm_temperature: float = 0.2             # recipes want determinism, not creativity
    max_llm_tokens: int = 2048

    # --- Guards ---
    provenance_gate: bool = True
    scrub_git_history: bool = False          # repo history is legitimate solve context

    # --- Shipping gate ---
    oracle_gate: bool = True                 # `harbor run -a oracle` must return 1.0
    oracle_timeout_sec: int = 0              # 0 ⇒ derive from the three budgets below
    oracle_build_slack_sec: int = 900        # image build + harbor overhead
    verifier_timeout_sec: int = 1800         # → task.toml verifier.timeout_sec
```

Notable knobs:

- **`min_target_tests` counts *passing* tests, not collected ones.** It applies to the F2P set derived from the green oracle run — a suite that collects 400 tests and passes 3 is a 3-test task.
- **`max_target_tests < min_target_tests` is legal.** The floor is corpus admission ("is this suite worth grading"); the cap is grading cost ("how much of it do we grade"). `min=5, max=3` means "only repos whose suite passes at least 5 tests, graded on 3 of them" — a coherent thing to want.
- **`oracle_timeout_sec=0` derives from the other three budgets** — `max_setup_time_sec + verifier_timeout_sec + oracle_build_slack_sec` — because the oracle gate wraps the *whole* `harbor run` subprocess (image build + solve + verify), which is strictly more than the two inner phases those budgets bound. A flat default would let a task using its full stated agent budget fail its own shipping gate.
- **`refs`** is additive to `repo.ref`, never a replacement for it — `repo.ref` is always a candidate, and each extra ref pays a full bootstrap. Multi-ref fan-out beyond an explicit list is out of scope for v1.
- **There is no `--force-language` interaction worth noting** — that flag downgrades a pipeline/language compatibility check that only `code_instruct` and `equivalence_tests` declare (`supported_languages`); `env_setup` is polyglot and doesn't declare it.

## Yield & repo suitability

Discovery isn't the bottleneck here the way it is for the mining pipelines — the candidate set is `[repo.ref] + refs`, not a history to filter. **Expected yield is high**: any repo where `bootstrap/` already produces a working image is a candidate. The binding filters are `min_target_tests` and the recipe-verification retry loop, not discovery.

- **What works** — CPU-only test suites, no exotic system deps. Python, JS, Go, Rust libraries — polyglot from day one, unlike `code_instruct`/`equivalence_tests`.
- **What doesn't** — GPUs, external services (a live database, a paid API), or setup that needs privileges outside a container (kernel modules, systemd, `sudo`).
- **The hidden cost axis is `refs`, not repo count** — one env per repo at HEAD is the v1 shape; each additional ref is a second full bootstrap, and `PipelineResult` has nowhere to report the running spend (`bootstrap_cost_usd` lives per-task in metadata instead).

## Reward kinds

| Kind | When emitted | What the trainer/agent sees |
|---|---|---|
| `test_execution` | Always | **Graded** `reward = f2p_rate` (P2P is always empty ⇒ `p2p_rate = 1.0`) in `reward.txt`; `resolved` (all F2P pass) + full breakdown in `reward.json`. Oracle = 1.0 by construction (F′ + the oracle gate). |

No `diff_similarity` fallback: there is no gold source-code diff to compare against — the oracle is a shell script to execute, not an edit to a solution file.

## What we reuse

| Source | Reused as |
|---|---|
| `bootstrap/` | The whole subsystem — the point of this pipeline is exposing it as a training target |
| `pipelines/_pr_runtime_verifier.py` | Shipped verbatim as `tests/verifier.py` — already graded, already polyglot, already degrades to exit-code fallback, already returns `p2p_rate = 1.0` on an empty P2P |
| `pipelines/_eval_script.py` | `normalize_test_cmds_for_runtime`, `_path_prelude_for_language` (both relocated here from `pr_runtime` to avoid an import cycle), plus new `env_prelude_from_test_cmds` and `authed_clone_url` (the latter shared with `pr_diff`, which adopts it to fix a live private-GitLab clone bug) |
| `sources.py` | `Capability.REMOTE_CLONE` (new) — the source-compatibility gate that keeps `file://` repos off this pipeline |
| `_env_guard.py` | Git-history scrub only, and only when `scrub_git_history=True` |

Studied, not vendored, for the overall task shape: [Repo2Run](https://github.com/bytedance/Repo2Run) (Python-only, doesn't publish the environment for training), [SetupBench](https://github.com/microsoft/SetupBench) (pass/fail scoring, not graded), [EnvBench](https://github.com/JetBrains-Research/EnvBench) (canonical-smoke pass/fail). None of them ships this as an RL training pipeline with a graded reward — see [RFC 0008](../rfcs/0008-env-setup.md) for the full comparison and design rationale.

## Open questions / promotion criteria

`env_setup` ships **experimental**. Before promoting it to stable, RFC 0008 calls for a real-agent eval (10 sampled envs via Harbor with `claude-code` + Sonnet, reported against Repo2Run's ~55% Python baseline) and, specifically, the **gate-0 failure rate on real agents** — how often a genuine agent takes the PyPI-substitution shortcut is the number that tells us whether the provenance gate was worth building. See the RFC's Rollout section for the full plan.
