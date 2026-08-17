# RFC 0008: `env_setup`

**Status:** implemented (experimental)
**Author:** `@adithya-s-k`
**Created:** 2026-07-22
**Last revised:** 2026-08-16 — merged the companion design spec into this document, swept the merged text for internal contradictions, then re-verified every code claim against the working tree and fixed six design defects the sweep had missed; see [Revision history](#revision-history).
**Target release:** v0.9.0
**Implemented by:** _(pending)_
**Reference dataset:** _(pending — target `AdithyaSK/repo2rlenv-env-setup`)_

This RFC is self-contained: it argues *why* `env_setup` should exist **and** pins *how* it is
built — module layout, options, the generation algorithm, the exact emitted task tree, the
verifier contract, and the test plan. Every claim about the current codebase in this document
was checked against the code; where a mechanism does not exist yet, the RFC says so and names
what has to be built.

---

## Summary

Take a bare, un-bootstrapped repo and hand the agent the RL task of *making the test suite build
and run green from scratch*. The emitted Harbor task starts as `git clone <repo>` at some
`base_commit` with **nothing installed** — no dependencies, no venv, no toolchain beyond the bare
language base image. The reward is the fraction of the repo's own tests that pass under whatever
install recipe the agent produces. **The pipeline turns our existing `bootstrap/` machinery —
today a build-time step consumed by every sandboxed pipeline — into a first-class training and
eval target.**

## Motivation

Every sandbox-verified pipeline we ship (`pr_runtime`, `commit_runtime`, `cve_patches`,
`code_instruct`, `equivalence_tests`, `pr_to_env`) leans on `bootstrap/` to produce a per-repo
Docker image where the test suite runs. Bootstrap itself is an agent — an LLM iterates commands
(`apt install`, `uv pip install -e ".[test]"`, `pytest --collect-only`) until the smoke gate
passes. That agent-with-shell loop is *itself* a great RL environment: (1) it's model-agnostic —
any coding agent can drive it; (2) it's naturally verifiable via test exit status; (3) the goal is
unambiguous — "make the tests pass"; (4) it stresses tool-use fluency more than pure code
generation, which is the current weak point in most agents.

Three papers converge on this task shape, and none of them is quite what we want to ship:

- **Repo2Run** (arXiv:2502.13681) — the closest prior work. Takes a Python repo with no Dockerfile
  and produces a runnable image. Uses an agent + rollback stack + pytest verification. Their eval
  dataset is small (~100 repos), Python-only, and they don't publish the environment for training.
- **SetupBench** (arXiv:2507.09063) — benchmarks 43 tasks across languages (Python, JS, Rust, Java,
  Go) for repo-setup capability. Scoring is "did the reference command succeed?" — not the graded
  partial-credit signal we want.
- **EnvBench** (arXiv:2503.14443) — 329 repos across languages, scored by a language-specific
  "canonical smoke test". Same limitation: pass/fail, not graded.

None of them ships the pipeline for training loops. Our `bootstrap/` layer *already does the hard
work* (docker sandbox primitives, language auto-detect, LLM-driven iteration, cost tracking) — this
RFC exposes that machinery as a `repo2rlenv generate` pipeline instead of a build step, gives it a
graded reward, and publishes a reference dataset.

### The anti-argument

*"Isn't this the same thing as our existing `bootstrap/` step? Why is it a pipeline?"* The
`bootstrap/` step is a **build-time producer** — it consumes an unbuilt repo and produces a cached
Docker image the other pipelines start from. It hides its work from the agent that eventually
solves the downstream task. `env_setup` is the **runtime consumer** — the agent-under-eval receives
the un-bootstrapped repo and *does* the bootstrap work. Same primitives, opposite direction. And it
needs its own emitter changes, its own gates, and its own reference dataset.

*"Isn't this an eval, not a training env?"* Both. As an eval it's a direct capability probe. As a
training env it exposes a domain (build-system fluency, package-manager idioms, dependency-conflict
resolution) where agents are weak and explicit reward signal is scarce.

---

## 1. Premise

The consequence that is easy to get wrong: **we still run bootstrap, but we throw its image away.**
What we keep is the transcript (raw material for the gold recipe), `test_cmds` (what "the suite"
means for this repo), and `language`. The emitted task's `FROM` is the *bare base image bootstrap
started from*, so the gold recipe replays against identical ground.

| Property | Value |
|---|---|
| `name` | `PipelineName.ENV_SETUP = "env_setup"` |
| `requires_bootstrap` | `True` (for the transcript, not the image) |
| `required_capabilities` | `frozenset({Capability.REMOTE_CLONE})` — mines no platform data, but the emitted Dockerfile must be able to clone the repo, which a `file://` source cannot serve (§2) |
| `supported_languages` | `None` — polyglot from v1 |
| `experimental` | `True` |
| `reward_kinds` | `["test_execution"]`, set in the `repo2env` metadata dict inside `_build_task` — where every pipeline sets it. It is **not** a ClassVar on any pipeline (§2). "Graded" is a *property* of that kind, not a kind; recorded as `reward_granularity` (§6) |
| Sandbox at generation time | Yes (Docker) |
| LLM at generation time | Yes — bootstrap, plus 1–3 distillation calls per `(repo, ref)` |
| LLM at run time | No — verifier is pure stdlib |

**Base image resolution.** `runner.py:547` resolves the base as `spec.base_image or
base_image_for(lang)`, where `base_image_for` is the table at `bootstrap/language.py:35-45`. It does
**not** read `bootstrap/presets.py`; `presets` is consumed only by `bootstrap/prompts.py:5` for the
system prompt. The two tables are independent literals that happen to agree today
(`python:3.12-slim`, `node:22-slim`, `golang:1.23`, `rust:1-slim`, `eclipse-temurin:21-jdk`,
`ubuntu:24.04` for C/C++ and unknown) — `env_setup` must read the same function bootstrap read,
`base_image_for`, not `PRESETS`, or the emitted `FROM` can silently differ from the one the recipe
was distilled against.

**`BootstrapResult` has no `base_image` field.** On the auto-detect path the only in-band record of
the resolved base is the `FROM` line inside `dockerfile_reconstruction` (built at `runner.py:709`;
`:426` is the `FROM` line *inside* the helper); the cache key stores `spec.base_image`, which is
`None` whenever detection chose it (`runner.py:66`). `env_setup` therefore recomputes the base **by
the same expression `runner.py:547` used** — `bare_base_image(spec, lang)` returning
`spec.base_image or base_image_for(lang)`, reading the same `BootstrapSpec` the bootstrap ran under
(§2) — and compares it against the `FROM` line of `dockerfile_reconstruction` when that string is
available. A mismatch **skips the ref** (`base_image_mismatch`, §9) rather than raising: an
`AssertionError` inside `run()` lands in the generic `build_failed` bucket and loses the diagnosis.

**Taking `spec` and not just `lang` is load-bearing.** A `bare_base_image(lang)` that consulted only
`base_image_for` would disagree with the reconstructed `FROM` on **every** ref whenever
`--base-image` is set: `--base-image alpine:3.20` makes bootstrap build `alpine:3.20` while
`base_image_for(PYTHON)` still answers `python:3.12-slim`, so the entire run would skip
`base_image_mismatch` and emit nothing. The override reaching the pipeline (§4, *Multi-ref
bootstrapping*) is what makes `spec.base_image` *readable* here; it is not by itself what makes the
two values agree — the shared expression is.

The one bootstrap path that can produce a genuine mismatch — `spec.user_dockerfile`, which returns
`language=UNKNOWN` (⇒ `ubuntu:24.04`) alongside a user-authored `FROM` we never see — is rejected by
its own explicit guard in §4, *not* left to be caught incidentally by the `verify_passed` default.

---

## 2. Module layout

Three new files; the touched set is larger than a first pass suggests.

### New

| File | Runs | Responsibility |
|---|---|---|
| `pipelines/env_setup.py` | generation | The pipeline. ~250 LOC. |
| `pipelines/_setup_recipe.py` | generation | Distill the bootstrap transcript into a clean `setup.sh`, then prove it in a container built from the exact Dockerfile we are about to emit. Retry loop. ~200 LOC. |
| `pipelines/_env_setup_lang.py` | generation | Per-language table: **provenance probe kind**, package/dist-name resolution, test-root discovery. ~150 LOC, mostly data. The bare base image is **delegated**: `bare_base_image(spec, lang)` is a one-line `return spec.base_image or base_image_for(lang)` — the same expression `runner.py:547` evaluated, which is what keeps `--base-image` runs off the `base_image_mismatch` skip (§1). This module contains **no image literals**: §1 exists to stop a second table, and a third one in here would be the same defect (§10 asserts the absence). |

**Follow the real pipeline convention.** `docs/contributing/ADDING_A_PIPELINE.md` documents it
accurately — filters are domain-named, "never a generic `_should_skip`" (`:350`), and discovery lives
in a fat `run()` (`:344`). Restated here because `env_setup` has to match what the seven registered
pipelines actually do:

- ClassVars: `name`, `requires_bootstrap`, `experimental`, optionally `required_capabilities` /
  `supported_languages`. **`reward_kinds` is not among them** — no pipeline declares it as a class
  attribute; it is a key each `_build_task` writes into its `repo2env` metadata dict (`pr_diff.py:555`,
  `pr_runtime.py:1110`, `code_instruct.py:507`, …), back-filled by the emitter (`harbor.py:89,91`).
  §1's table row and §10's test are about that emitted value, not an introspectable attribute.
- `__init__(input, options, bootstrap=None)` — the third parameter is part of the Protocol
  (`base.py:90-95`) and `cli.py:263` always passes it.
- `set_progress_callback` + `_emit_progress` — every pipeline implements this pair; `cli.py:267-268`
  wires it duck-typed via `hasattr`. It is not in the Protocol, and omitting it silently costs the
  live progress view.
- A fat `run(out_dir) -> PipelineResult` with discovery inlined, plus domain-named filters
  (`_pre_filter`, `_structural_quality_filter`, `_metadata_filter`, …) and `_build_task(...) -> HarborTask`.

`env_setup` declares `required_capabilities: ClassVar[frozenset[Capability]] =
frozenset({Capability.REMOTE_CLONE})`. It mines no PRs, issues, or commit API — but `frozenset()`
would wave a `file://` source through the pre-flight at `cli.py:120` and into a `docker build` that
cannot reach a host path, failing deep inside the build with `repository not found`. The existing
`Capability` members model *platform data* only, so §15's exclusion of local repos had nowhere to be
enforced; `REMOTE_CLONE` gives it one, and the pre-flight's error already names the compatible
pipelines for the given source.

The pipeline **also** re-checks the source in `__init__` and raises with the same wording. Pipelines
are constructible without the CLI — `docs/reference/API.md` shows exactly that — so the pre-flight is
a nicer error, not the guard.

### Touched

| File | Change |
|---|---|
| `emitter/harbor.py` | **Four changes** — see below. |
| `pipelines/_eval_script.py` | Receives `normalize_test_cmds_for_runtime` (moved from `pr_runtime.py:618-704`), `_path_prelude_for_language` (moved; **and used** — see §7's `test.sh` head), `env_prelude_from_test_cmds` (new, §7), and `authed_clone_url` (new, §6 — the shared GitHub/GitLab clone-URL builder). Note the module has no `__all__` and already carries four `equivalence_tests`-specific AST helpers; these four land beside them. |
| `sources.py` | Add `Capability.REMOTE_CLONE` — "a URL a `docker build` can clone from". Granted to `GITHUB` and `GITLAB`, **not** `LOCAL`. This is what makes §15's exclusion of `file://` repos enforced rather than documented: the `cli.py:120` pre-flight already prints the missing capability *and* the list of pipelines that do work on the given source. |
| `pipelines/pr_diff.py` | Switch its hardcoded `str.replace("https://github.com/", …, 1)` (`:209-212`) to `authed_clone_url`. Not incidental cleanup: `pr_diff` has claimed GitLab support since v0.8.4, and on a GitLab URL the replace is a no-op, so the `if [ -n "$GITHUB_TOKEN" ]` branch clones the **unauthenticated** URL and a private MR-mined task fails with a misleading error. Default `arg_name="GITHUB_TOKEN"` keeps `pr_diff`'s emitted Dockerfile byte-identical for github.com. |
| `pipelines/pr_runtime.py` | Re-exports `normalize_test_cmds_for_runtime` — `cve_patches`, `commit_runtime`, `pr_to_env`, and `tests/test_pipeline_pr_runtime.py` all import it from `pr_runtime` today. |
| `pipelines/_pr_runtime_verifier.py` | Promote `_detect_runner` (`:202`) to public `detect_runner` and add it to `__all__` (currently `["grade", "main", "parse_cargo_test", "parse_go_test", "parse_jest", "parse_logs", "parse_pytest"]`). Generation-time F2P derivation (§4) calls it. |
| `spec/input.py` | Add `ENV_SETUP = "env_setup"` to `PipelineName`. |
| `spec/options.py` | Add `EnvSetupOptions`, register in `OPTIONS_REGISTRY`. |
| `pipelines/__init__.py` | Register `EnvSetupPipeline` in `PIPELINES` and in `__all__`. |
| `registry/integration.py` | `_PUBLIC_DOCKER_HUB_BASES` must accept every base `base_image_for` can return (§14, *Publish*). The `eclipse-temurin:` gap is closed at `:125` — **in the working tree only, not yet committed**, so it lands with this pipeline rather than ahead of it. §10's `test_public_base_covers_every_language` is what keeps it closed. |
| `tests/test_emitter.py` | Constructs `HarborTask` directly (`:12`); covers the new optional fields. |
| Docs | See the itemized list below — it is longer than "add a row" in several places. |

#### The docs set, itemized

Three of these are not "add a row", which is why the table above delegates:

- `docs/pipelines/env_setup.md` — new page.
- `mkdocs.yml` — nav row, format `"env_setup (experimental)": pipelines/env_setup.md` (`:147-154`).
  Note `pr_to_env` is **already** missing from that nav; fix it in passing or leave it, but don't
  assume the list is complete.
- `docs/pipelines/README.md` — **four** pipeline tables, not three: `:24` (capability matrix), `:49`
  (yield), `:166` (reward kinds), `:189` (contamination posture) — **plus** the count sentence at
  `:22` ("All 7 pipelines are shipped — 3 stable … 4 experimental"). The `:49` yield table is
  already missing `pr_to_env`, so it needs two rows.
- **`docs/reference/SPEC.md`** — two edits, not one. A sentence under the `test_execution` row
  (`:210`) that the float may be graded or binary per pipeline, *and* a definition for
  **`reward_granularity`**, which appears nowhere in SPEC.md today. §6 introduces it; the reward-kind
  list itself stays at two (`:205`).
- `docs/reference/AUTH.md` — the build-time-clone section (`:68-77`) is scoped to `pr_diff` **by
  name** and documents only the `x-access-token:…@github.com` form. It needs rewriting to be
  pipeline-plural and to cover `oauth2:…@gitlab.com` (§6) alongside the `GIT_TOKEN` build arg — not
  just a new row.
- `docs/reference/REWARD_SCHEMA.md` — per-pipeline summary table (`:167-174`), also missing
  `pr_to_env`.
- `docs/reference/API.md:119` and `docs/contributing/ADDING_A_PIPELINE.md:224` — both construct a
  `HarborTask`; both need the field reorder from the emitter change below.
- **Pipeline counts** — eight places say six or seven: `docs/pipelines/README.md:22`, `CLAUDE.md:230`,
  `ADDING_A_PIPELINE.md:242` and `:352`, `docs/index.md:66` and `:83`, `REWARD_SCHEMA.md:167-174`,
  and this RFC's own "all seven registered pipelines" above. `docs/index.md:66` ("six pipelines") and
  `:83` ("Four more pipelines in the RFC queue") are already stale from `pr_to_env` shipping.
- `docs/index.md`, `README.md`, `docs/reference/BOOTSTRAP.md`, `CLAUDE.md` — prose updates.

#### `emitter/harbor.py` — four changes, one of which is not additive

`HarborTask` is `@dataclass(slots=True)` (not frozen) with fields in this order: `name`, `org`,
`description`, `instruction`, `oracle_diff`, `repo2env`, `difficulty="medium"`, `category="bugfix"`,
`keywords=field(default_factory=list)`, `environment_dockerfile=None`, `test_script=None`,
`aux_files=field(default_factory=dict)`.

1. **`solve_script: str | None = None`.** Today `solve.sh` is hardcoded to `git apply patch.diff`
   (`harbor.py:149-160`); our oracle must *execute* a recipe, not apply a source patch. When set,
   write it verbatim as `solution/solve.sh` (mode `0o755`); when `None`, keep today's shim.
2. **`oracle_diff: str | None = None` — this one requires a move.** `oracle_diff` is field 5 of 12
   and the field immediately after it, `repo2env` (`harbor.py:52`), has **no default**. Giving
   `oracle_diff` a default in place raises `TypeError: non-default argument 'repo2env' follows
   default argument` at class-creation time, i.e. the package fails to import. Either relocate
   `oracle_diff` into the defaulted tail (after `keywords`), give `repo2env` a `default_factory`, or
   declare the dataclass `kw_only=True`. A reorder is source-compatible: there are **eight**
   construction sites — `pr_diff.py:587`, `pr_runtime.py:1120`, `pr_to_env.py:705`,
   `commit_runtime.py:614`, `code_instruct.py:534`, `equivalence_tests.py:686`, `cve_patches.py:569`,
   and `tests/test_emitter.py:12` — and **all eight pass every field by keyword**, `oracle_diff`
   included. Two doc snippets also construct it (`docs/reference/API.md:119`,
   `docs/contributing/ADDING_A_PIPELINE.md:169`) and must be updated with it.
   Then: when `oracle_diff is None`, skip writing `solution/` entirely — both `solution/patch.diff`
   and `solution/solve.sh` are written unconditionally today (`harbor.py:140-160`), unlike
   `environment/Dockerfile` and `tests/test.sh` which are already guarded, so both writes move
   inside the guard. `_content_hash` (`harbor.py:67-72`) hashes `task.instruction` + `b"\0"` +
   `task.oracle_diff` and calls `.encode()` on it directly, so it needs `task.oracle_diff or ""` or
   it raises `AttributeError` on the new shape.
3. **`agent_timeout_sec: float | None = None`.**
4. **`verifier_timeout_sec: float | None = None`.**

   (3) and (4) exist because `harbor.py:132-133` writes `agent.timeout_sec = 1800.0` and
   `verifier.timeout_sec = 300.0` as **bare literals** in the payload dict. There is no per-task
   field and no post-emit rewrite — the only task.toml mutator after emission is
   `registry/integration.py:295-341`, which touches `spec_version`, `reproducibility`, and the legacy
   `bootstrap_image` and round-trips everything else. Without these two fields, `max_setup_time_sec`
   and `verifier_timeout_sec` in §3 are unimplementable and every emitted task ships the 300 s
   verifier budget that §3 argues is too short. Both fields fall back to today's literals when
   `None`, so no existing pipeline changes behavior.
   (`pr_to_env._run_oracle_gate` passes `oracle_timeout_sec` to `subprocess.run(timeout=...)` around
   `harbor run` — that bounds our harness process, not the emitted `agent.timeout_sec`, and is not a
   workaround for this.)

Two notes on the `_eval_script.py` move:

- `_eval_script.py:14` currently imports `_path_prelude_for_language` **from** `pr_runtime`. Adding a
  `pr_runtime → _eval_script` import closes that into a cycle. Move `_path_prelude_for_language`
  into `_eval_script.py` in the same change and have `pr_runtime` re-export it; a re-export that only
  works because of definition ordering inside a partially-initialized module is not a design, it's a
  fuse.
- **Name collision.** `repo2rlenv.log_parsers` defines its own `_detect_runner` and `parse_logs`
  with *incompatible* signatures — `log_parsers._detect_runner(test_cmds: list[str])` and
  `log_parsers.parse_logs(test_cmds: list[str], log: str, *, language: str | None = None)` versus the
  verifier's `detect_runner(test_cmds: str)` and `parse_logs(runner: str, log: str)`. First
  positional is a list of commands in one and a runner string in the other. Import the verifier's
  aliased and never let both names be live in one module.

### Deliberately *not* new

There is **no `_env_setup_verifier.py`.** The emitted `tests/verifier.py` is
[`_pr_runtime_verifier.py`](https://github.com/huggingface/Repo2RLEnv/blob/main/src/repo2rlenv/pipelines/_pr_runtime_verifier.py)
byte-for-byte, shipped as a plain file (not base64 — `pr_runtime._verifier_source` just
`read_text()`s it and `_runtime_aux_files` writes it to `tests/verifier.py`). It is already graded,
already polyglot (pytest / `go test` / `cargo test` / jest), already degrades to an exit-code reward
on unparseable logs, and already returns `p2p_rate = 1.0` when the P2P list is empty — which is
exactly `reward = f2p_rate`. The things it does not do — the provenance gate and the tamper restore
— live in `test.sh` *ahead* of it (§7), so the verifier stays shared and unforked.

#### The verifier's runner argument is load-bearing

`--runner` (declared at `_pr_runtime_verifier.py:321`, applied at `:330`) takes precedence:
`runner = args.runner.strip() or _detect_runner(args.test_cmds)`. `pr_runtime`'s emitted `test.sh`
passes only `--test-cmds` (`pr_runtime.py:535-538`), so runner selection happens at *run* time there.
**`env_setup` passes `--runner` explicitly**, and the reason is not that we know the runner by some
better means — we resolve it with the *same* heuristic on the *same* string,
`detect_runner(" ".join(cmds))` (§4). The gain is that we resolve it **once, at generation time, where
`unknown` is observable and actionable**. At run time it is neither: `detect_runner("")` returns
`unknown`, `parse_logs("unknown", …)` returns `{}`, and the verifier falls into the exit-code fallback
whose `--exit-code` default is `1` — graded reward silently becomes binary reward.

So the invariant is: resolve the runner in step H, and if it is `unknown`, **skip the ref**
(`runner_undetectable`, §9) rather than baking a value the verifier cannot use. Without that check an
unresolvable runner produces an empty parse, zero passing tests, and a skip labelled
`too_few_tests` — a misleading diagnosis for a repo whose suite may be perfectly healthy. We pass
`--runner`, `--test-cmds`, `--exit-code`, and `--out-dir`; dropping any of them produces a wrong
reward, not a crash.

---

## 3. Options

```python
class EnvSetupOptions(_BaseOptions):
    """Repo2Run/SetupBench-style: agent makes a bare repo's test suite run green.

    Unlike every other sandboxed pipeline, the emitted environment deliberately
    has NO dependencies installed — the bootstrap image is used only to derive
    the gold recipe and is then discarded.
    """

    # --- Discovery ---
    limit: int = 20
    refs: list[str] | None = None            # commits/tags to base tasks on; None ⇒ HEAD only

    # --- Signal floors ---
    min_target_tests: int = 5                # reject suites too small to grade meaningfully
    max_target_tests: int = 0                # cap the F2P set; 0 ⇒ whole suite
    max_setup_time_sec: int = 1800           # agent budget → task.toml agent.timeout_sec (§2)

    # --- Oracle recipe (§5) ---
    emit_solution: bool = True               # False ⇒ eval-only split; distillation still runs
    max_recipe_attempts: int = 3             # TOTAL attempts, not retries-after-the-first
    recipe_verify_timeout_sec: int = 1800
    llm_temperature: float = 0.2             # recipes want determinism, not creativity
    max_llm_tokens: int = 2048

    # --- Guards (§7) ---
    provenance_gate: bool = True
    scrub_git_history: bool = False          # repo history is legitimate solve context

    # --- Shipping gate ---
    oracle_gate: bool = True                 # `harbor run -a oracle` must return 1.0
    oracle_timeout_sec: int = 0              # 0 ⇒ derive (see the invariant below)
    oracle_build_slack_sec: int = 900        # image build + harbor overhead
    verifier_timeout_sec: int = 1800         # → task.toml verifier.timeout_sec (§2)

    @model_validator(mode="after")
    def _check_target_bounds(self) -> "EnvSetupOptions":
        # An empty F2P list makes grade() return reward=0.0, resolved=False
        # unconditionally (§4), so the floor is what keeps that unreachable.
        if self.min_target_tests < 1:
            raise ValueError("min_target_tests must be >= 1: an empty F2P set always scores 0.0")
        if self.max_target_tests < 0:
            raise ValueError("max_target_tests must be >= 0 (0 ⇒ whole suite)")
        return self
        # NB: max_target_tests < min_target_tests is legal and meaningful — see below.

    @property
    def effective_oracle_timeout_sec(self) -> int:
        if self.oracle_timeout_sec:
            return self.oracle_timeout_sec
        return self.max_setup_time_sec + self.verifier_timeout_sec + self.oracle_build_slack_sec

    @model_validator(mode="after")
    def _check_oracle_timeout(self) -> "EnvSetupOptions":
        floor = self.max_setup_time_sec + self.verifier_timeout_sec + self.oracle_build_slack_sec
        if self.oracle_timeout_sec and self.oracle_timeout_sec < floor:
            raise ValueError(
                f"oracle_timeout_sec={self.oracle_timeout_sec} < {floor}: a task using its full "
                "stated budget could not pass its own oracle gate"
            )
        return self
```

**This is the first options class with validators.** `spec/options.py` imports only
`BaseModel, ConfigDict` today (`:9`) and no existing options class uses `model_validator` or a
`@property` — the string does not appear in the file. Cross-field constraints live in pipeline
`__init__`s by convention (see `PrToEnvOptions`' comment at `:269`: *"Exactly one of `url` /
`urls_file` must be set. Enforced in pipeline __init__."*). `EnvSetupOptions` breaks that convention
deliberately, because both invariants below are about the options *alone* and want to fail at parse
time rather than after a bootstrap has been paid for. The change adds `model_validator` to the
module's pydantic import — worth noting since §2's touched-files row would otherwise read as additive.

**`language_hint` is dropped.** The override already exists: `--language` on the `generate`
subparser (`cli.py:886-888`) sets `BootstrapSpec.languages_hint`, which `runner.py:540-546` reads
before falling back to `detect_language()`. Note this is **not** `--force-language`
(`cli.py:906-912`), which is a `store_true` that merely downgrades the pipeline/language
compatibility mismatch from an exception to a warning (`base.py:100-142`) — and is inert for
`env_setup` anyway, since the check only runs when `supported_languages is not None` and the source
is GitHub, and only `code_instruct` and `equivalence_tests` declare `supported_languages`.

**`limit` semantics differ from the mining pipelines.** There is no over-fetch: the candidate set is
`[repo.ref] + [r for r in (refs or []) if r != repo.ref]`, truncated to `limit` (§4, *Multi-ref
bootstrapping*, defines it and says why `repo.ref` is always first). `env_setup` is not mining
history, it is enumerating build targets. With `refs=None` the candidate set is `[repo.ref]` and
`limit` is inert — kept for CLI symmetry with the mining pipelines, not because the default
configuration reads it.

**`min_target_tests` counts *passing* tests, not collected ones.** It applies to the F2P set derived
from the green oracle run (§4), which is why it is not named `min_tests_collected`: a suite that
collects 400 tests and passes 3 is a 3-test task, and the floor should say so.

**The floor and the cap are independent, and `max_target_tests < min_target_tests` is legal.**
`min_target_tests` is a *corpus-admission* floor — "is this suite worth grading at all" — while
`max_target_tests` is a *grading-cost* cap on the F2P set we bake. `min=5, max=3` therefore means
"only repos whose suite passes at least 5 tests, graded on 3 of them", which is a coherent thing to
want. An earlier draft's validator rejected that combination with "every task would be skipped", which
is only true under truncate-then-floor — the ordering §4 explicitly rejects. Worse, the validator made
that ordering unobservable: when `max ≥ min` and `len ≥ min`, `min(len, max) ≥ min` always holds, so
no legal configuration could ever distinguish the two orders. The validator is gone; the floor that
`grade()` actually needs (`min_target_tests ≥ 1`) took its place.

**`oracle_timeout_sec` must cover the budgets it wraps.** It bounds the whole `harbor run` subprocess
(the shape `pr_to_env._run_oracle_gate` uses: `subprocess.run(timeout=...)`), which spans image build
+ oracle solve + verifier — while `max_setup_time_sec` and `verifier_timeout_sec` bound only the two
inner phases and already sum to 3600 s at defaults. A flat `1800` meant a task using its stated budget
could not pass its own gate. So the invariant is stated and enforced:

> `oracle_timeout_sec ≥ max_setup_time_sec + verifier_timeout_sec + oracle_build_slack_sec`

with `0` meaning "derive it", which is the default and tracks §14 step 5's p99 tuning of
`verifier_timeout_sec` automatically. The derived value is a ceiling, not an expectation: the oracle's
measured `oracle_setup_time_sec` (§6) is far below the agent budget, because the recipe is distilled
and already proven green.

**`max_setup_time_sec` and `verifier_timeout_sec` are separate for a reason.** The agent's recipe
runs during the *agent* phase, but `tests/test.sh` re-runs the whole suite during the *verifier*
phase from an already-installed state. Harbor's default 300 s verifier budget is a coin flip on any
real repo. §14 sets the default from measured `oracle_test_time_sec` p99 rather than from this
paragraph.

**`emit_solution` replaces the RFC draft's `use_oracle_recipe`.** The rename is not cosmetic — see
§5.

**Probe vocabulary.** `probe ∈ {direct_url, path, none}`, spelled the same way in options,
`provenance.json`, and metadata. `provenance_gate` (bool) records whether the option was on;
`provenance_probe` (str) records what actually shipped after the degradation ladder ran (§7). There
is no `"n/a"`.

**`provenance_gate=False` bakes `probe="none"`.** The emitted gate 0 branches on the *probe* value in
`provenance.json`, not on the option — the option does not survive into the container. So turning the
gate off means writing `probe="none"` (which gate 0 treats as a no-op) and skipping the F′ probe
ladder entirely; the still-emitted `provenance_gate=false` in metadata is what records that the
`none` was chosen rather than degraded into. Without this, `provenance_gate=False` would be silently
inert.

---

## 4. Generation algorithm

```mermaid
flowchart TD
    A[repo @ ref] --> B[ensure_bootstrap]
    B -->|raises / smoke or verify failed| SK1[skip: bootstrap_failed]
    B -->|user_dockerfile bootstrap| SK8[skip: bootstrap_source_unsupported]
    B --> BI{"bare_base_image(spec, lang) == reconstructed FROM?"}
    BI -->|no| SK9[skip: base_image_mismatch]
    BI --> BP[normalize + drop blank test_cmds]
    BP -->|empty| SK5[skip: no_runnable_test_cmds]
    BP --> C[build task Dockerfile:<br/>bare base + clone@ref, NO installs]
    C --> D[LLM distill: transcript → setup.sh]
    D -->|no source| SK6[skip: no_recipe_source]
    D --> E[docker build that exact Dockerfile]
    E --> F[run: bash setup.sh && test_cmds<br/>xtrace off during capture]
    F -->|red| G{attempts left?}
    G -->|yes| D
    G -->|no| SK2[skip: recipe_unverified]
    F -->|green| TC{tracked tree still clean?}
    TC -->|dirty| G2{attempts left?}
    G2 -->|yes| D
    G2 -->|no| SK10[skip: recipe_edits_tracked_files]
    TC -->|clean| RN{"detect_runner(joined cmds)"}
    RN -->|unknown| SK11[skip: runner_undetectable]
    RN --> H[parse log via verifier parse_logs → target test set]
    H -->|< min_target_tests| SK3[skip: too_few_tests]
    H --> FP[F′: dry-run the emitted test.sh in that container;<br/>degrade probe to none on gate-0 failure]
    FP -->|still not 1.0| SK7[skip: gates_unverified]
    FP --> I[emit Harbor task]
    I --> J{emit_solution and oracle_gate?}
    J -->|yes| K[harbor run -a oracle]
    K -->|reward != 1.0| SK4[drop: oracle_gate_failed]
    K -->|1.0| Z[task shipped]
    J -->|no| Z
```

### B′ — normalize, then drop blanks

```python
cmds = [c for c in normalize_test_cmds_for_runtime(bootstrap.test_cmds) if c.strip()]
```

**The filter is now belt-and-braces, and that is a change from an earlier draft.**
`normalize_test_cmds_for_runtime` (`pr_runtime.py:618-704`) used to be strictly 1:1 — one
unconditional `out.append(cleaned.strip())` per input — which mattered because its strip regexes
(`:657-660`) remove `| head -50`, `2>&1`, `&>/dev/null`, and trailing `" |&"` characters *before*
runner detection, so an input of `"| head -50"` or `"2>&1"` reduced to `""` and was appended verbatim.
Downstream that became an empty segment in a `" && ".join(...)`: a bash syntax error, not a no-op.

The function now drops those itself — an early `continue` at `:665-666` and a guarded append at
`:701-703`, with the non-index-alignment documented at `:639-645`. Verified: the input
`["| head -50", "2>&1", "  ", ". /workspace/.venv/bin/activate && pytest -v"]` returns exactly
`['. /workspace/.venv/bin/activate && pytest -v']`. **That fix is uncommitted at the time of writing**,
so `env_setup` keeps its own `if c.strip()` guard: it is redundant against the working tree, one
character of insurance against the function reverting, and free. What is *not* free is relying on the
old 1:1 property — an `env_setup` that assumed index alignment with `bootstrap.test_cmds` would now be
wrong.

Bootstrap records fast sanity commands — `presets.py:63-66` suggests
`python -m pytest --collect-only -q | head -50` — and a task needs commands that actually *run*
tests and emit per-test lines. Everything downstream (recipe verification, the F2P parse, the
emitted `test.sh`, `metadata.test_cmd`) uses this normalized, filtered list. Never the raw one.

An empty result skips: `no_runnable_test_cmds`. Not hypothetical —
`_bootstrap_from_user_dockerfile` (`runner.py:314-407`, taken whenever `spec.user_dockerfile` is
set) returns `test_cmds=[]`, `rebuild_cmds=[]`, `language=UNKNOWN`, `transcript_path=None`, a
hardcoded `smoke_passed=True`, and — the field it never sets — `verify_passed=False`
(`bootstrap/spec.py:44`). That default is the gate the path actually trips, which is why the next
section rejects it by name instead of relying on it.

### Gate the bootstrap on `verify_passed`, not just `smoke_passed`

`ensure_bootstrap` **raises** `BootstrapError` for: bootstrap disabled (`runner.py:461`), no Docker
daemon (`:463`), private repo with no token (`:471`), clone/fetch/checkout failure
(`:175`/`:199`/`:209`/`:221`), a failed `git rev-parse HEAD` (`:49`), a missing `--user-dockerfile`
(`:331`), the user-Dockerfile build's own failure (`:371`), and agent-loop failure (`:609-613`) —
budget exhaustion arrives through that last one as `outcome.success=False` with
`reason="cost budget exceeded: …"`, not as a distinct raise. It returns `smoke_passed=False` in exactly
one case (`:621-631`): `test_cmds` is non-empty **and** the joined script's exit code is outside
`(0, 1, 5)`. With `test_cmds` empty, `smoke_ok` stays `True`.

The stronger signal is `verify_passed` (called at `runner.py:671-673`, warning at `:678-682`, stored at
`:726-727`): a replay of the same commands in a **fresh `docker run --rm` container from the committed
tag**, which catches state the agent set in the live shell that `docker commit` dropped. Today nothing
consults it — and nothing even *displays* it: it appears only in `bootstrap/spec.py:44`,
`runner.py:726`, and one test. (`cli.py:832` prints `smoke_passed`, not `verify_passed`.) `env_setup`
must gate on it: a recipe distilled from a transcript whose install did not survive `docker commit` is
a recipe that will not reproduce.

**Neither flag is a signal when `test_cmds` is empty.** `_verify_committed_image` short-circuits to
`(True, "(no test_cmds recorded — skipped)")` at `runner.py:269-270`, so a bootstrap that recorded zero
test commands reports `smoke_passed=True` **and** `verify_passed=True` having executed nothing. That is
not a hole in this gate so much as a reason B′ has to run independently of it: `no_runnable_test_cmds`
is what actually catches that bootstrap, and the ordering in the flowchart (bootstrap gates → B′) means
the vacuous `True` is never load-bearing.

Also note `DockerError` (not `BootstrapError`) can propagate uncaught from `DockerSandbox.start`
(`docker.py:216-294`) and `commit` (`:330-333`) — `runner.py` wraps neither, `cmd_generate` catches only
`BootstrapError` (`cli.py:224`), and `main()` has no top-level handler, so today it surfaces as a
traceback. Per-ref bootstraps inside the pipeline must catch both.

### Reject the `user_dockerfile` bootstrap by name

A `verify_passed=False` default is not a gate, it is a coincidence that currently points the right
way. `env_setup` checks the source explicitly — `bootstrap.extra.get("source") == "user_dockerfile"`
⇒ skip `bootstrap_source_unsupported` — for a reason that outlives the default: that path gives us
`language=UNKNOWN` (⇒ `base_image_for` returns `ubuntu:24.04`) next to a user-authored `FROM` we
never parse, so the §1 base-image agreement cannot be established even if everything else about the
bootstrap were sound. The recipe would then be distilled against one base and replayed against
another.

This also settles §5's fallback ordering: with the path rejected up front, `spec.user_dockerfile`
is *not* a live justification for the `dockerfile_reconstruction` fallback.

### The load-bearing invariants

Step E builds **the exact `environment/Dockerfile` we are about to emit** — same base, same clone,
same ref. Not a lookalike, not a reconstruction. This buys four things:

1. The oracle recipe is verified against the environment it will actually run in, so
   `harbor run -a oracle == 1.0` is true *by construction*, not by hope.
2. The F2P set is produced by the same run that proves the oracle, **and by the same parser that
   will grade it**: step H calls
   `_pr_runtime_verifier.parse_logs(detect_runner(" ".join(cmds)), log)` directly rather than
   reimplementing a parse. There is no separate snapshot pass to drift out of sync. The runner
   resolved here is also the one baked into `test.sh` as `--runner` (§2), so generation and grading
   agree on it by construction — and an `unknown` result skips the ref (`runner_undetectable`)
   instead of being baked as a value the verifier would ignore.
3. The gates are dry-run against that known-good container (F′) and degraded until they pass, so we
   never ship a gate we have not watched succeed on a correct solve.
4. The normalized `test_cmds` list from B′ is the single source for recipe verification, the F2P
   parse, the emitted `test.sh`, and `metadata.test_cmd`.

If you refactor this pipeline, keep steps B′→F′ welded together. Splitting them is how this design
breaks.

**Capture the log the same way at generation time and at run time.** This does *not* happen for
free, and "cannot diverge by construction" is false if you ignore it. `pr_runtime`'s emitted script
runs `set -uxo pipefail` and captures with `( … ) > test_output.log 2>&1` — xtrace goes to stderr,
stderr is redirected into the parsed log, so `+ cmd` lines land in the file being parsed. The pytest
parser tolerates them (a `+ pytest -v` line has no status token, so it is dropped), but the **jest
parser does not**: after a `PASS <file>` header, any unmatched non-empty line that is not on its skip
list is pushed onto the describe stack (`_pr_runtime_verifier.py:190-198`) and prefixed onto every
subsequent test id. Reproduced: with a `+ npx jest --ci` trace line interleaved, the id becomes
`src/foo.test.ts > + npx jest --ci > Foo > returns 200` instead of `src/foo.test.ts > Foo > returns 200`
— which then matches nothing in the baked F2P set. Since `env_setup`'s F2P is the *whole suite*, that
is proportional reward loss, not an edge case.

`pr_runtime` does not solve this either. `pr_runtime_validate._slice_test_output` (`:176-190`) is
sometimes cited as stripping the trace; it does not — it slices between `R2E_START_TEST_OUTPUT` /
`R2E_END_TEST_OUTPUT` markers and relies on `ExecResult.truncated()` concatenating stdout before
stderr (`bootstrap/docker.py:36-44`), so the markers are found in the stdout copy. That protection
exists only at generation time, has no run-time counterpart, and the same stdout-only slicing would
drop the output of any runner that reports on stderr (jest's default reporter does).

`env_setup` therefore specifies one capture shape for both sides: **xtrace is disabled around the
graded run** (`set +x` … `set -x`, §7), and step F captures with the same shape.

### F′ — dry-run the gates

Unconditional, and it dry-runs the **whole emitted `tests/test.sh`** (gate 0 → gate ½ → gate 1), not
just the probe, inside the same green container from step F. The bar is `reward == 1.0`. On a gate-0
failure, **degrade the probe to `none`** — a single step from whatever the language's probe was — re-run,
and record the shipped value in `provenance.json` and `metadata.provenance_probe`. There is no
intermediate rung to try; §7 shows why `path` cannot serve as one for Python. On a gate-½ or gate-1
failure the ref is skipped (`gates_unverified`) rather than degraded: those gates have no weaker form
that is still worth shipping.

This is where "the oracle scores 1.0" is actually established. It runs even when `oracle_gate=False`
and even when `emit_solution=False`, because it is not a shipping gate — it is how the probe gets
chosen and how the script gets proven. A gate that cannot pass on a known-good environment is a gate
that would zero every agent.

**What F′ does and does not establish.** It runs `test.sh` in the post-`setup.sh` container *without
re-running `setup.sh`*, so it does catch a recipe that depends on an in-tree edit: gate ½ reverts the
edit, gate 1 goes red, reward ≠ 1.0, `gates_unverified`. An earlier draft claimed F′ "inherits the same
blind spot" as the recipe and therefore says nothing about tracked-file edits — that was wrong, and
step F's tracked-tree assertion (§5) is not justified by it. What that assertion buys is **diagnosis**:
without it the symptom is an opaque `gates_unverified` at the end of the pipeline, and with it the
symptom is `recipe_edits_tracked_files` plus a retry carrying the one complaint that can actually fix it
("install from the tree as it is"). Same detection, far better failure mode, one `git diff --quiet`.

The residual F′ genuinely cannot cover is **solve shapes unlike the oracle's**. `env_prelude.sh` is
derived from the *bootstrap's* `test_cmds` (§7), so it activates the environment the oracle used. An
agent that installs somewhere else — its own venv at a different path, a different interpreter — gets
a prelude that does not activate its work, and gate 0 may fail for a solve that is otherwise correct.
F′ cannot see that, because it only ever watches the oracle succeed. §14 step 5's gate-0 failure rate
on real agents is the measurement that would expose it; that is the number to watch before promoting
out of `experimental`.

It generically covers what a hand-written special case would miss: namespace packages, repos whose
import name differs from the package directory, `src/` layouts, installers that do not write PEP 610
metadata, and Node repos that do not self-resolve their own name from `/workspace`.

### Target test set selection

The F2P set is every test the shared parser reported as `PASSED` in step F. `SKIPPED` is dropped
explicitly — an agent that installs nothing still "passes" a skipped test, so counting them hands
out free reward.

There is no `XFAIL`/`XPASS` handling to write: the parsers normalize into exactly four statuses —
`PASSED`, `FAILED`, `SKIPPED`, `ERROR` — and xfail outcomes never enter the map (verified: the
strings appear nowhere in `src/`, and a pytest log line carrying `XFAIL` parses to `{}`). Only the
pytest parser can even emit `ERROR`; go, cargo, and jest map three statuses each. That is a property
of the shared parser, not a filter we apply.

**Order matters: floor first, then truncate.** Apply `min_target_tests` to the full passing set,
*then* `max_target_tests` (stable sort by test id, so re-runs are idempotent). The two questions are
asked in that order because they are about different things: the floor asks *is this repo gradeable*
(a property of the suite), the cap asks *how much of it do we grade* (a property of our budget).
Truncating first would let the cap answer the floor's question — `min_target_tests=5` with
`max_target_tests=3` would skip every task in the corpus instead of grading 3 tests from suites that
pass at least 5. That configuration is legal (§3): the ordering here is what makes it mean something.

**P2P is empty (`[]`, not "names + statuses").** There is no pre-existing passing set to protect;
the agent starts from zero. `grade()` maps an empty P2P list to `p2p_rate = 1.0`, so
`reward = f2p_rate` falls out with no verifier change. Conversely `grade()` returns
`reward=0.0, resolved=False` when the **F2P** list is empty (`:274,:277`) — `min_target_tests ≥ 1`
is what makes that state unreachable.

Flakiness degrades gracefully rather than catastrophically: one flaky test in a 400-test suite costs
`1/400` of the reward, not the whole task. That is the main reason this design prefers a large F2P
set over a curated small one.

### Multi-ref bootstrapping

`cmd_generate` calls `ensure_bootstrap()` once, for `input.repo.ref` (`cli.py:172-233`), and returns
`1` on `BootstrapError` — so a bootstrap failure on the primary ref is fatal before the pipeline is
constructed. For every *additional* entry in `refs`, the pipeline calls `ensure_bootstrap()` itself.
Four specifics the algorithm depends on:

- **"Additional" is defined against `repo.ref`, and `repo.ref` is always a candidate.** The candidate
  set is `[repo.ref] + [r for r in (refs or []) if r != repo.ref]`, truncated to `limit`; `refs`
  entries are *in addition to* the primary ref, never instead of it. Anything else wastes the
  bootstrap `cmd_generate` already paid for — the most expensive single step in this pipeline — on a
  ref no task uses. Dedup on the **resolved SHA**, not the ref string: `RepoSpec.ref` defaults to
  `"HEAD"` (`spec/input.py:27`), so `refs=["<sha-of-HEAD>"]` would otherwise bootstrap the same tree
  twice and emit two task directories with the same id (§6 names tasks by resolved SHA).

- **The ref is passed through `repo`, not as an argument.** `ensure_bootstrap` reads `repo.ref`, so a
  per-ref call needs `input.repo.model_copy(update={"ref": r})` — the one thing that actually varies.
- **CLI bootstrap overrides now reach the pipeline.** `--language`, `--base-image`,
  `--max-spend-usd`, and `--bootstrap-opt` were applied to a local `model_copy` that was never
  written back to `gen_input.bootstrap`, so a pipeline reading `self.input.bootstrap` got the
  un-overridden spec and extra refs would have bootstrapped on different ground than the primary
  ref — breaking the byte-identical-ground invariant this section is built on. Fixed in
  `cli.py:199-202` — the overridden spec is written back with
  `gen_input.model_copy(update={"bootstrap": bspec})`, and because `model_copy` is shallow,
  `gen_input.bootstrap is bspec` is the same object `ensure_bootstrap` receives at `:213-214`. **That
  fix is an uncommitted working-tree change**, so it lands with this pipeline. Two related caveats
  remain: `--bootstrap-opt` applies values via
  `model_copy(update=...)`, which **bypasses validation**, so a field declared `Path` can hold a
  `str`; and `--max-spend-usd` caps bootstrap LLM spend only — no pipeline reads it.
- **Per-ref bootstrap spend has nowhere to be reported.** `PipelineResult` (`base.py:37-53`) carries
  `candidates`, `emitted`, `skipped`, `out_dir`, `skip_reasons` — and no cost field. Each extra ref
  costs a *full bootstrap*, the expensive axis of this pipeline. v1 records
  `bootstrap_cost_usd` in per-task metadata and logs the running total; adding a cost field to
  `PipelineResult` is a cross-pipeline change we are not making here, and it is a second reason §15
  keeps multi-ref fan-out out of v1.

---

## 5. The gold recipe

Neither bootstrap artifact is usable as-is, which is the single biggest gap between the original RFC
sketch and a working implementation:

- **`BootstrapResult.dockerfile_reconstruction`** replays *every* `BASH` action from the agent
  transcript — including failed attempts, `grep`s, `ls`es, and `pytest` invocations
  (`runner.py:431-434` replays them unconditionally). `_reconstruct_dockerfile`'s docstring
  (`runner.py:411-422`) concedes it is not always reproducible; the field itself carries only an
  inline comment (`bootstrap/spec.py:41`). Shipped as an oracle it would produce envs whose gold
  recipe scores below 1.0.
- **`BootstrapResult.rebuild_cmds`** is narrower: commands to re-apply a build *after a patch*, on an
  image where installation already happened. Not a from-scratch recipe.

So `_setup_recipe.py` distills and then proves.

### Recipe source, in preference order

1. `transcript_path`'s `BASH` turns.
2. `dockerfile_reconstruction` + `rebuild_cmds` when the transcript is absent — **older cache entries
   predate transcript linking** (`transcript_path` is attached post-hoc at `runner.py:731-737`), so a
   cache hit on a pre-v0.9 entry has a usable image record and no transcript. That is the only
   surviving justification: an earlier draft also cited `spec.user_dockerfile` "skipping the agent loop
   entirely", but §4 now rejects that bootstrap source by name before distillation is reached, so it
   cannot produce this fallback. Expect this rung to be cold on fresh runs; it exists for cache age,
   not for a live code path.
3. Neither available → skip, `no_recipe_source`.

The distillation prompt doesn't care where the raw material came from, because the recipe is proven
by execution either way (step F).

### Distillation

One LLM call **per attempt** (1–3 total, bounded by `max_recipe_attempts`). Input: the ordered `BASH`
commands with exit codes and truncated output, plus the normalized `test_cmds`, plus the base image.
Output: a single `setup.sh`. The prompt's contract on the model:

- Emit **one** bash script, `set -euo pipefail`, runnable from `/workspace`.
- Include only commands on the successful path. Drop diagnostics, dead ends, and the test invocation
  itself (the harness runs `test_cmds` separately).
- Consolidate: one `apt-get update && apt-get install -y ...` rather than five.
- Preserve ordering constraints the transcript reveals (codegen before install,
  `SETUPTOOLS_SCM_PRETEND_VERSION` before an editable install, `editable_mode=compat` for namespaced
  packages, …) — these are enumerated in `bootstrap/presets.py`'s `known_pitfalls`, passed through
  verbatim.
- No `pip install <the repo's own released package>`. Install from `/workspace`. (Gate 0 enforces
  this at run time; the prompt states it here because the *oracle* must not do it either.)
- **Do not modify files tracked in the repository.** Gate ½ restores the tracked tree before grading
  (§7), so a recipe that depends on an in-tree edit scores 0 on its own task. Same reason as the rule
  above: the oracle has to live inside the contract the environment enforces.

### Verification and retry

Build the emit-candidate Dockerfile, run `bash setup.sh && <test_cmds>` under
`recipe_verify_timeout_sec`, capture the log with xtrace off.

**Green is necessary but not sufficient: the tracked tree must also still be clean.** After `setup.sh`
runs, assert `git -C /workspace diff --quiet HEAD --` (the trailing `--` keeps untracked files out of
the verdict — verified: an untracked-only working tree exits 0). A recipe that edits a tracked file —
pinning a dep in `pyproject.toml`, adding an ini option to `tox.ini` — passes step F and then trips
gate ½, which restores the tracked tree before grading (§7).

**This is a diagnostic check, not the only line of defence.** F′ already catches such a recipe: it
re-runs `test.sh` without re-running `setup.sh`, gate ½ reverts the edit, gate 1 goes red, and the ref
skips `gates_unverified` (§4). What the explicit check buys is a *usable* failure: a dirty tracked tree
feeds the retry loop the one complaint that can fix it ("do not modify files tracked in the repository;
install from the tree as it is"), and exhaustion skips with its own reason,
`recipe_edits_tracked_files`, instead of an opaque `gates_unverified` several steps later. Detecting the
same fault twice, earlier and by name, is worth one `git diff`.

- **Green, tree clean** → this is the oracle. Parse the log for the target test set.
- **Red** → feed the last ~4 KB of combined stdout/stderr back with the previous `setup.sh` and ask
  for a corrected script. Capped at `max_recipe_attempts` (default 3, **total**, matching
  `max_attempts_per_seed` in `code_instruct` and `max_attempts_per_function` in
  `equivalence_tests`).
- **Exhausted** → skip the ref, `recipe_unverified`, and dump the attempts to
  `<out_dir>/.debug_skips/<task_id>/` (the convention `equivalence_tests` established in v0.8.7).

Cost: 1–3 LLM calls at ≤2048 tokens plus 1–3 container runs — small next to the bootstrap call that
precedes it, but *per `(repo, ref)`*, and each additional `refs` entry pays a **full bootstrap**.

### `emit_solution=False`

The draft RFC called this `use_oracle_recipe=False` and said it "skips distillation entirely." That
is an algorithm hole: the only route to the F2P set is distill → build → run green → parse. Kill
distillation and there is no green run, hence no F2P set, no `min_target_tests` check, and no
container in which to dry-run the gates (F′) — the task cannot be emitted at all.

So the flag governs **emission, not generation**. Distillation, the green run, the F2P parse, and F′
all run exactly as normal; the pipeline simply does not write `solution/` — which is why
`HarborTask.oracle_diff` has to become optional (§2). `oracle_gate` is forced off (there is no
`solution/solve.sh` for `harbor run -a oracle` to execute; the proof of correctness is F′ instead),
and `[metadata.repo2env.env_setup]` records `oracle="none"`. Useful for a held-out eval split where
the recipe should not exist anywhere in the artifact.

---

## 6. Emitted task tree

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
│   ├── p2p.json            # []
│   ├── provenance.json     # {language, probe, package, dist_name, base_commit}
│   ├── provenance_read.py  # one-shot reader: emits probe + base_commit
│   ├── provenance_run.sh   # dispatches to the right probe under the prelude
│   ├── provenance.py       # Python probe (shipped when language == python)
│   ├── provenance.js       # Node probe (shipped when language == node)
│   └── test_roots.json     # pathspecs gate ½ cleans of untracked files (not tree-filtered)
└── solution/               # omitted entirely when emit_solution=False
    ├── patch.diff          # a diff that creates /workspace/setup.sh
    └── solve.sh            # git apply patch.diff && bash /workspace/setup.sh
```

The task id uses the **resolved SHA**, not the ref string —
`f"{owner}__{name}-envsetup-{resolved_sha[:12]}"`, matching `commit_runtime.py:549`'s convention.
`HEAD` is not a stable id; two runs a week apart would collide on one directory name while
describing different trees.

### `environment/Dockerfile`

Follows `pr_diff`'s build-time-clone pattern (`pr_diff.py:195-258`): `ARG GIT_TOKEN=` defaulted
empty, an authenticated clone URL built by injecting the build-arg token after the scheme,
`git clone --filter=blob:none`, then `git remote set-url origin <clean url>` so no credential
persists in a layer, then `git reset --hard <base_commit>` and `git clean -fdx`.

**The URL comes from the shared `authed_clone_url` helper, and GitLab is in v1.** `pr_diff` builds it
with `repo_url.replace("https://github.com/", "https://x-access-token:${GITHUB_TOKEN}@github.com/", 1)`
(`:209-212`) — on a `gitlab.com` URL that replace is a no-op, so the `if [ -n "$GITHUB_TOKEN" ]` branch clones
the *unauthenticated* URL and a private clone fails with an error that blames the repo rather than the
token. Since `env_setup` runs on any source with `Capability.REMOTE_CLONE` (§2), that would be a
shipped-broken half of its input surface. `authed_clone_url(repo_url, arg_name=...)` in
`_eval_script.py` handles `x-access-token@github.com` and `oauth2@gitlab.com`, and `pr_diff` adopts it
with `arg_name="GITHUB_TOKEN"` so its emitted Dockerfile stays byte-identical for github.com while its
GitLab claim (v0.8.4) becomes true. `env_setup` uses the source-neutral `GIT_TOKEN` because it is not a
GitHub-only pipeline; §14 step 2's polyglot smoke gains a private-GitLab case.

Four deliberate differences from `pr_diff`:

1. `FROM` is the bare language base from `base_image_for(lang)` — the same one bootstrap started
   from (§1).
2. **No dependency installation whatsoever.** The toolchain layer installs only `git`,
   `ca-certificates`, `curl`, and `python3` (the last purely so `verifier.py` can run on non-Python
   bases). Installing the repo's own dependencies here would delete the task. This is the single
   mistake that silently voids the pipeline, and §10 has a dedicated regression test for it.
3. **No sentinel injection and no baseline commit.** Earlier drafts injected a `__r2e_setup_sentinel__`
   attribute into the package root and then committed it so the marker survived git operations. Both
   are gone — see §7, where PEP 610 metadata replaces the sentinel outright. This removes three
   defects at once: the forgeability of a marker committed into the tree the agent is editing; the
   package-root heuristic and its `sentinel_injection_failed` skip; and a build-breaking `git commit`
   with nothing staged (`git commit` with a clean index **exits 1**, verified — and only Python would
   have injected anything, so the build would have died for five of six languages with an unexplained
   `build_failed`).
4. Because `git reset --hard <base_commit>` + `git clean -fdx` already leave a **clean tree at the
   resolved SHA**, that SHA is the restore anchor for gate ½ directly. There is no separate
   `sentinel_commit`, and nothing to keep the two in sync.

`git_history_scrub()` is called only when `scrub_git_history=True`. Default off: the repo's
`CONTRIBUTING.md`, `.github/workflows/ci.yml`, and commit history are legitimate solve context for a
setup task, not a leak. Either way `base_commit` stays reachable, so gate ½ works with the scrub on
or off.

For Node there is no `package.json` edit at all, which removes the `npm ci` lockfile-integrity hazard
that editing it would have created — as a side effect rather than as a patch.

### `instruction.md`

Templated — no LLM call. Content: the repo and ref, the fact that nothing is installed, the goal
("make the project's test suite run and pass"), the working directory, the time budget, and an
explicit statement that installing system and language packages is permitted and expected.

It does **not** name the target tests, name the package manager, or hint at the recipe. It does not
mention the provenance gate — *the environment enforces, the prompt never asks*.

**One deliberate exception, and it is the tamper restore.** The instruction carries exactly one
sentence about it:

> Your solution must not depend on modifications to files tracked in the repository; the repository's
> tracked files are restored before grading.

This is a disclosed *contract*, not a hint about the reward, and the distinction is what makes it an
exception worth making rather than a hole in the principle. Gate ½ restores the whole tracked tree
(§7), which is not negotiable — the pytest config surface is precisely where grading is gamed, and
Go/Rust F2P ids carry no file, so there is no narrower surface to restore. Real Repo2Run-style setup
work does sometimes include tracked-file edits (pin a dep, drop a `use_2to3`, add an ini option to
`tox.ini`), and the subset of those whose effect is read *at test time* silently scores 0. Withholding
that fact does not buy uncontaminated evaluation; it buys an unfair task and a confusing transcript.
An agent told the rule can satisfy it — install from the tree as it is — without being told anything
about the F2P set, the probe, or the reward shape.

The cost of the restore is recorded in §7 and §13; this sentence is how the agent learns about it.

It also must **not** repeat the draft RFC's phrasing, which told the agent to "write
`environment/Dockerfile` … when Harbor rebuilds and runs `pytest`." Harbor builds
`environment/Dockerfile` **once**, then runs the agent and `tests/test.sh` inside that same
container; it never rebuilds an agent-edited Dockerfile. (A rebuild would need docker-in-docker or a
second Harbor pass and would break on the Modal / Daytona / E2B backends.) The agent sets up the
**live container**; `test.sh` grades in place. That sentence was copy-pasteable prompt text and is
the most dangerous kind of stale doc.

### `solution/`

`patch.diff` is a unified diff that creates `/workspace/setup.sh` (via
`_eval_script.make_unified_diff`). `solve.sh` applies it and runs it:

```bash
#!/bin/bash
set -euxo pipefail
cd /workspace
git config --global --add safe.directory /workspace
git apply --verbose --reject "$(dirname "$0")/patch.diff"
bash /workspace/setup.sh
```

**`patch.diff` creates `setup.sh`; it does not execute it.** Unlike every other pipeline in the set,
applying the gold patch alone scores 0 — the recipe is an artifact to *run*, not an edit to apply.
`solve.sh` is the executable oracle and is what `harbor run -a oracle` uses; that is what forces the
new `HarborTask.solve_script` field (§2), since today's `solve.sh` is a hardcoded `git apply` shim.
Consumers that ingest only `solution/patch.diff` get the full recipe text (useful as an SFT target)
but not a self-applying fix. **We lose SWE-bench parity here**, and the dataset card should say so
rather than implying otherwise.

Keeping the recipe inside `patch.diff` rather than inlining it into `solve.sh` still buys exactly one
canonical oracle artifact, with `solve.sh` as a thin shim over it.

### `[metadata.repo2env.env_setup]`

| Key | Type | Meaning |
|---|---|---|
| `language` | str | `LanguageHint` value from bootstrap detection |
| `base_image` | str | The bare `FROM` |
| `test_cmd` | str | The **normalized** command `test.sh` runs (joined `test_cmds`) |
| `runner` | str | `pytest` \| `go` \| `cargo` \| `jest` — passed to the verifier as `--runner` |
| `target_test_count` | int | `len(f2p)` |
| `recipe_lines` | int | Non-comment lines in the gold `setup.sh` — a crude difficulty proxy |
| `recipe_attempts` | int | Distillation attempts needed (1 = clean first pass) |
| `oracle_setup_time_sec` | float | Wall-clock for the gold `setup.sh` |
| `oracle_test_time_sec` | float | Wall-clock for the suite run *after* setup — the evidence `verifier_timeout_sec` gets tuned on |
| `agent_time_budget_sec` | int | `max_setup_time_sec`, mirrored for dataset-card queries |
| `base_commit` | str | Resolved SHA; the anchor gate ½ restores from |
| `has_lockfile` | bool | A `uv.lock` / `poetry.lock` / `package-lock.json` / `Cargo.lock` / `go.sum` exists at `ref` |
| `bootstrap_cost_usd` | float | LLM spend for this ref's bootstrap + distillation |
| `provenance_gate` | bool | Was the option on |
| `provenance_probe` | str | What actually shipped: `"direct_url"` \| `"path"` \| `"none"` |
| `reward_granularity` | str | `"graded"` — the property `reward_kinds` must not encode (§1). `"binary"` exists for the pipelines that are; nothing here emits it |
| `oracle` | str | `"recipe"` \| `"none"` |

The F2P list itself lives in `tests/f2p.json`, where the verifier already reads it — not duplicated
into the TOML as `target_tests`. A several-hundred-element list in two places gains nothing and gives
the copies a way to disagree; `target_test_count` is the queryable summary.

`has_lockfile` answers the RFC's lockfile question: a repo with a lockfile is substantially easier
(`uv pip sync uv.lock` is nearly the whole task), so training regimes need to rebalance. It costs one
`git ls-tree`, so we record it rather than debate it.

---

## 7. Verification

### `tests/test.sh` — head

Mirrors `pr_runtime.build_eval_script`'s head (`pr_runtime.py:498-516`) with two additions — a
`write_reward` helper (seven call sites across the three gates want one, where `pr_runtime` inlines a
single `echo`) and the environment prelude — and it **keeps** `pr_runtime`'s PATH prelude rather than
dropping it.

```bash
#!/bin/bash
set -uxo pipefail
<_path_prelude_for_language(language)>   # may be empty; see below
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd /workspace
git config --global --add safe.directory /workspace
mkdir -p /logs/verifier

write_reward() {
  printf '%.6f\n' "$1" > /logs/verifier/reward.txt
  printf '{"reward":%s,"resolved":false,"parse_status":"%s"}' "$1" "${2:-verifier_crashed}" \
    > /logs/verifier/reward-details.json
}

# Both gates run under the same environment the test commands run under.
# Sourced, never interpolated — see below.
set +u; . "$SCRIPT_DIR/env_prelude.sh"; set -u
```

Four details that are easy to get wrong:

- **The PATH prelude is not optional, and `env_prelude.sh` does not replace it.** `pr_runtime` emits
  `_path_prelude_for_language(language)` immediately after `set -uxo pipefail` (`pr_runtime.py:501`,
  helper at `:406`) precisely because the bootstrap agent installs toolchains into
  `/usr/local/go/bin`, `$HOME/.cargo/bin`, or an nvm dir without persisting an `export PATH` — and
  Harbor runs `bash test.sh` non-interactively, so `go test` / `cargo test` / `node` exit **127** and
  the reward is a false 0. The two preludes solve different halves: `_path_prelude_for_language` is a
  static per-language PATH fix, `env_prelude.sh` is the venv/export fragments recovered from
  *this repo's* `test_cmds`. `env_setup` needs the static one **more** than `pr_runtime` does, since
  here the toolchain is installed by the agent under evaluation rather than baked into the image.
  This is why §2 relocates the helper into `_eval_script.py` and §12 lists it as reused.

- **The prelude is a shipped file, not an interpolated string.** Earlier drafts exported it as
  `R2E_ENV_PRELUDE='<fragments>'` and `eval`'d it. That breaks twice over: real bootstrap `test_cmds`
  carry quoted values (`export PYTEST_ADDOPTS='-p no:randomly' &&`), so a single `'` terminates the
  shell string — a syntax error at best and emit-time shell injection from LLM-produced bootstrap
  output at worst; and fragments extracted with their trailing `&&` make
  `eval ". …/activate &&"` a syntax error which, with `set -e` absent, is swallowed, leaving the venv
  simply not activated. Shipping `tests/env_prelude.sh` and sourcing it removes the quoting layer
  entirely. `env_prelude_from_test_cmds(test_cmds) -> str` (new in `_eval_script.py`) extracts the
  leading environment-setup fragments — `. <path>/activate`, `source …`, `export FOO=bar` —
  **strips the trailing `&&`/`;` from each**, and joins them with newlines, emitting `true` when
  there are none. §10 asserts the stripped shape.
- **`set +u` around the source.** Activation scripts written by older virtualenv/venv touch unbound
  variables (`PS1`); with `set -u` on, sourcing one aborts the gate. This is not a corner case:
  `presets.py:53-55` explicitly instructs the bootstrap agent that "every `test_cmd` entry must
  include `. /workspace/.venv/bin/activate &&` as a prefix" when a venv is unavoidable, so
  bootstrap-recorded `test_cmds` carrying that prefix is a documented, expected shape. Without the
  prelude, a recipe that installs into a venv passes gate 1 (which inherits activation from
  `test_cmds`) and fails gate 0 (which would not).
- `${2:-...}` in `write_reward` is not decoration — `set -u` is on and the gate-1 fallback calls
  `write_reward 1.0` with one argument. **The default is `verifier_crashed`, deliberately not
  `fallback_exitcode`:** that second string is what `verifier.py` writes when it runs fine but cannot
  parse the log (`_pr_runtime_verifier.py:349`), and reusing it would make "the verifier died" and "the
  verifier degraded gracefully" indistinguishable in the artifact — the same two-postures-in-one-script
  defect gate 0 avoids below. `resolved` is hardcoded `false` at all seven call sites, which is correct:
  every one is a failure or degraded-fallback path, including the `write_reward 1.0` case (exit code 0
  with an unparseable log and an F2P oracle present is exactly where `verifier.py` itself refuses to
  claim `resolved`). The real `resolved` bool comes from `verifier.py`, which overwrites both files on
  the happy path.

### Gate 0 — provenance

With egress open — and it must be open, `pip`/`apt`/`cargo` are the task — an agent can run
`pip install click`, install the *released* package into site-packages, and watch the repo's own
tests pass green against it. Reward 1.0, and the source tree in `/workspace` was never made
installable. The task was not solved; it was sidestepped. This is the shortcut an agent finds first,
and it is the one real contamination hole in this task shape.

```bash
PROV="$(python3 "$SCRIPT_DIR/provenance_read.py" "$SCRIPT_DIR/provenance.json" 2>/dev/null)" || {
  write_reward 0.0 provenance_unreadable; exit 0; }
{ read -r R2E_PROBE; read -r R2E_BASE_COMMIT; read -r R2E_LANG; } <<< "$PROV"
[ -n "$R2E_PROBE" ] && [ -n "$R2E_BASE_COMMIT" ] && [ -n "$R2E_LANG" ] \
  || { write_reward 0.0 provenance_unreadable; exit 0; }

case "$R2E_PROBE" in
  none) : ;;
  *)    bash "$SCRIPT_DIR/provenance_run.sh" "$R2E_LANG" "$R2E_PROBE" \
          || { write_reward 0.0 package_not_from_source; exit 0; } ;;
esac
```

**One read of `provenance.json`, and it fails closed.** Every value derived from that file — probe,
base commit, language — comes out of a single `python3` invocation, and the language and probe are
*passed as arguments* to `provenance_run.sh` rather than re-read there. An earlier draft had
`provenance_run.sh` do its own inline `python3 -c` read of the same file with `|| exit 1`, which meant
two reads with two different failure postures in one script; that is exactly the defect this gate is
built to avoid, so the second read is gone. (Gate ½ still reads a *different* file,
`test_roots.json` — one read each of two files, not two reads of one.)

The fail-closed posture is the point. An earlier draft read the probe with no `2>/dev/null` and no
`||`: a missing `python3` or unreadable JSON produced an empty substitution, fell to `*)`, ran
`provenance_run.sh`, whose own read was also empty, whose `case` hit `*) exit 0` — the gate silently
*passed*. Twelve lines later the identical read failed closed. This version picks fail-closed
everywhere and gives it a distinct `parse_status`.

`tests/provenance_read.py` — the one emitted file whose exact contract gate 0 depends on:

```python
"""Emit probe, base_commit, language — one line each, in that order.

Exit non-zero on anything unreadable so gate 0's `||` fires. Pure stdlib.
"""
import json, sys

try:
    cfg = json.load(open(sys.argv[1]))
    out = [cfg["probe"], cfg["base_commit"], cfg["language"]]
except Exception:
    sys.exit(1)

# A missing/blank value, or one containing a newline, would desync the three
# `read -r` calls in gate 0 and silently shift base_commit into $R2E_LANG.
if not all(isinstance(v, str) and v.strip() and "\n" not in v for v in out):
    sys.exit(1)

print("\n".join(out))
```

The probes ship as **files**, not heredocs — that is what makes `test_test_sh_gate0_golden` a test of
behavior rather than of string formatting. `provenance_run.sh` dispatches by language under the
prelude, taking both values from its caller:

```bash
#!/bin/bash
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LANG_ID="$1"; R2E_PROBE="$2"
set +u; . "$SCRIPT_DIR/env_prelude.sh"; set -u
case "$LANG_ID" in
  python) exec python3 "$SCRIPT_DIR/provenance.py" "$SCRIPT_DIR/provenance.json" "$R2E_PROBE" ;;
  node)   exec node    "$SCRIPT_DIR/provenance.js" "$SCRIPT_DIR/provenance.json" ;;
  *)      exit 0 ;;
esac
```

#### The Python probe: PEP 610, not a sentinel

`tests/provenance.py`:

```python
"""Exit 0 iff the package under test came from /workspace.

argv[2] is the probe rung: "direct_url" (PEP 610 metadata OR import location)
or "path" (import location only). Gate 0 never invokes this with "none".
"""
import importlib, importlib.metadata as md, json, sys

cfg = json.load(open(sys.argv[1]))
probe = sys.argv[2] if len(sys.argv) > 2 else cfg.get("probe", "direct_url")


def from_workspace_dist(dist_name: str) -> bool:
    # PEP 610: installers write <dist-info>/direct_url.json for a local / VCS / URL
    # install and MUST NOT write it for an index (PyPI) install.
    try:
        raw = md.distribution(dist_name).read_text("direct_url.json")
    except Exception:
        return False
    if not raw:
        return False
    try:
        return json.loads(raw).get("url", "").startswith("file:///workspace")
    except Exception:
        return False


def from_workspace_import(import_name: str) -> bool:
    try:
        m = importlib.import_module(import_name)
    except Exception:
        return False
    paths = [m.__file__] if getattr(m, "__file__", None) else list(getattr(m, "__path__", []))
    return any(
        p and p.startswith("/workspace/")
        and "/site-packages/" not in p and "/dist-packages/" not in p
        for p in paths
    )


if probe == "direct_url":
    # dist_name may be absent when only the import name could be determined at
    # emit time; `or ""` keeps that a clean False rather than a KeyError.
    ok = from_workspace_dist(cfg.get("dist_name") or "") or from_workspace_import(cfg["package"])
else:  # "path" — the weaker rung: import location only, no dist metadata required
    ok = from_workspace_import(cfg["package"])

sys.exit(0 if ok else 1)
```

**The rung selects the check, and the probe kind is per-language — not a three-step ladder.** An
earlier draft ran `from_workspace_dist(...) or from_workspace_import(...)` unconditionally and never
read `probe`, so `direct_url` and `path` were literally the same program and degrading between them
rewrote a JSON string to no effect. Reading the rung fixes that, and it also makes the arithmetic
visible: `path` is import-location-only while `direct_url` is dist-metadata-**OR**-import-location, so
`path`'s check is a strict *subset* of `direct_url`'s and **can never pass where `direct_url` failed**.
Verified: for a non-editable `pip install .` (metadata present, import resolving into site-packages)
`direct_url` exits 0 and `path` exits 1 — the two rungs differ, but in the wrong direction for a
fallback.

So there is no `direct_url` → `path` degradation, and never was a coherent one. The probe kind is a
property of the **language** (§7's table: Python `direct_url`, Node `path`, everything else `none`), and
the only degradation is a single step from that language's probe to `none`. `provenance.py` still
branches on the rung, for two reasons that outlive the ladder: the emitted probe kind is then always the
one actually enforced, and the file stays correct if a task is ever emitted with either value.

On the `direct_url` rung the two checks are an **OR**, and each covers what the other misses:

| Solve shape | `direct_url.json` | import path under `/workspace` | verdict |
|---|---|---|---|
| `pip install -e .` | `file:///workspace`, `dir_info.editable` | yes | pass |
| `pip install .` (system-wide) | `file:///workspace` | no (site-packages) | pass |
| `pip install .` into `/workspace/.venv` | `file:///workspace` | no (venv site-packages) | pass |
| `uv pip install -e .` | `file:///workspace` | yes | pass |
| no install; `PYTHONPATH=/workspace` | absent | yes | pass |
| `pip install <released package>` | **absent** | no | **fail** |

Verified on this machine: an editable local install carries
`{"url":"file:///…","dir_info":{"editable":true}}`, while PyPI-installed dists (`rich`, `pydantic`)
have no `direct_url.json` at all; `md.distribution(name).read_text("direct_url.json")` returns the
JSON or `None`, pure stdlib, no third-party import.

This replaces the sentinel design outright, and that is the point. The sentinel was
`__r2e_setup_sentinel__` appended to the package root and committed into the tree — but the tree is
the thing the agent is editing, so the bypass was `grep -r __r2e_setup_sentinel__ /workspace` (the
value also sat in `/tests/provenance.json`) plus a two-line append to the installed `__init__.py`.
That is one step past the shortcut, not determined forgery. The PEP 610 check is uniform across
editable/non-editable and system/venv, is not cheaply forgeable (an agent would have to hand-write
plausible dist-info metadata), and it deletes the sentinel injector, the package-root heuristic, the
`sentinel_injection_failed` skip reason, and the empty-commit build failure along with it.

`dist_name` (the distribution name, e.g. `Django`) and `package` (the import name, e.g. `django`) are
**both baked at emit time** from `pyproject.toml` / `setup.cfg` / `package.json`. Do not try to
recover the mapping at run time: `importlib.metadata.packages_distributions()` misses `.pth`-style
editable installs (verified — it omitted this repo's own editable install).

The two names fail differently. A missing **`dist_name`** costs nothing on the `direct_url` probe — the
OR falls through to the import check — so it does not degrade (verified: no `KeyError`, and the probe
still passes an editable install). A missing **`package`** (the import name) leaves the probe with
nothing to check on either rung, so it ships `none`. Only the import name is load-bearing.

**Honest limits.** The gate blocks the shortcut an agent stumbles into, not an adversary who
fabricates metadata inside a container they control. A gate that survives adversarial editing of the
container is not available to us and we do not claim one. And if some installer does not write PEP
610 metadata, F′ catches it on a known-good container and degrades to `path` before shipping.

#### The Node probe

`tests/provenance.js` is unchanged from the draft and is correct as written:

```javascript
const fs = require("fs");
const path = require("path");
const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
let resolved;
try {
  resolved = fs.realpathSync(require.resolve(cfg.package, { paths: ["/workspace"] }));
} catch (e) {
  process.exit(1);
}
const inWorkspace = resolved.startsWith("/workspace" + path.sep);
const inNodeModules = resolved.split(path.sep).includes("node_modules");
process.exit(inWorkspace && !inNodeModules ? 0 : 1);
```

**The `node_modules` exclusion is the whole Node probe.** `npm i <released-pkg>` lands inside
`/workspace/node_modules`, so a naive "is it under `/workspace`" check passes the exact shortcut it
is meant to block.

On failure the gate writes `reward.txt = 0.0` and `reward-details.json` with
`parse_status="package_not_from_source"`, then exits cleanly. It never crashes the harness.

#### Per-language posture

The `probe` column is this table's reason to exist. The base-image column is reproduced **for
orientation only** — it is what `base_image_for` returns today, `bootstrap/language.py:35-45` is the
single source of truth (§1), and `_env_setup_lang.py` delegates to it rather than restating it (§2). If
this column and the code disagree, the code wins and this column is stale.

| Language | Bare base (via `base_image_for` — illustrative) | `probe` | Mechanism |
|---|---|---|---|
| Python | `python:3.12-slim` | `direct_url` | PEP 610 `direct_url.json` OR import path under `/workspace` |
| Node | `node:22-slim` | `path` | `require.resolve` → `realpathSync` under `/workspace` and **not** under `node_modules/` |
| Go | `golang:1.23` | `none` | compiles from source by construction |
| Rust | `rust:1-slim` | `none` | compiles from source by construction |
| Java | `eclipse-temurin:21-jdk` | `none` | v1: no probe |
| C/C++, unknown | `ubuntu:24.04` | `none` | v1: no probe |

Go and Rust get `none` on purpose, not out of laziness: `go test ./...` and `cargo test` compile from
the `/workspace` source tree by construction, so the substitute-the-released-package shortcut does not
exist there. Java and C/C++ get `none` for the opposite reason — we have no probe we would trust, and
recording `none` in metadata is more honest than shipping a gate that checks nothing and calling it
coverage.

Degradation: **the language's probe → `none`**, one step, taken when the probe fails its dry-run against
the known-good oracle container (§4, F′). **We never ship a probe we haven't seen pass.** There is no
intermediate rung — `path` is a subset of `direct_url`, not a weaker version of it (above), so a Python
task whose probe fails degrades straight to `none` instead of through a rung that would fail
identically. Node degrades from `path` to `none` for the same reason: one probe, one fallback.

### Gate ½ — restore the graded tests

```bash
# Restore every tracked file, so an agent can't rewrite a graded test body to
# `assert True` and collect full reward.
git -C /workspace checkout "$R2E_BASE_COMMIT" -- . || { write_reward 0.0 test_restore_failed; exit 0; }

# Then remove untracked additions across the test + config surface — a restore
# of tracked files cannot delete a file the agent *added*. Pathspecs that match
# nothing are fine here (verified: `git clean` exits 0), so the list is emitted
# unfiltered — see below.
mapfile -t R2E_ROOTS < <(python3 -c 'import json,sys;[print(p) for p in json.load(open(sys.argv[1]))]' \
                           "$SCRIPT_DIR/test_roots.json")
if [ "${#R2E_ROOTS[@]}" -gt 0 ]; then
  git -C /workspace clean -fdq -- "${R2E_ROOTS[@]}" || { write_reward 0.0 test_restore_failed; exit 0; }
fi
```

**Restore the whole tracked tree, not a computed path list.** The draft derived `protected_paths.json`
from the baked F2P ids — the part before `::` for pytest, the `PASS <file>` path for jest, the package
dir for `go test` — and fell back to conventional roots (`tests/`, `test/`, `spec/`, `t/`) when the
ids carry no file. Both halves fail:

- `git checkout <sha> -- tests/ test/ spec/ t/` **fails the whole operation if any pathspec matches
  nothing**, and essentially no repo has all four. Verified: with only `tests/` present,
  `git checkout <sha> -- tests/ spec/` exits 1 with `pathspec 'spec/' did not match any file(s)`.
  That is `write_reward 0.0 test_restore_failed` for every task taking the fallback — including for
  the oracle, so those repos would be silently dropped at generation by `oracle_gate`.
- The fallback is not rare. The go parser emits **bare `TestFoo` names with no file or package**
  (`_pr_runtime_verifier.py:116-129`) and the cargo parser emits `module::path` with no file
  (`:132-145`) — so **every Go and Rust task** takes it.
- `git checkout <sha> --` with no pathspec at all is not the harmless no-op it looks like: verified,
  it exits 0 and **detaches HEAD onto that commit**, silently changing repo state instead of
  restoring anything.

`-- .` always matches (verified, exits 0), works identically for every runner, needs no test-id
parsing, and moots the Go/Rust gap entirely. A correct solve requires no in-tree edits — the recipe
is a `setup.sh`, not a source patch — so restoring tracked files costs a correct agent nothing. F′
proves that on the oracle before the task ships.

**Untracked files need `git clean`, and the list is emitted _unfiltered_.** A restore of tracked files
does not delete files the agent *added*. A new `/workspace/conftest.py` with a
`pytest_runtest_makereport` hook is easier than editing a test body and would survive a
tracked-file-only restore — and it is the exact case an earlier draft's `git ls-tree` filter let
through: roots were kept only if they **already existed**, so a repo with no `conftest.py` at
`base_commit` never emitted that pathspec and the added file was never cleaned. The filter defeated its
own motivating example.

The asymmetry that draft had already established is why the filter was unnecessary to begin with:
`git clean` accepts pathspecs that match nothing and exits 0 (verified), while `git checkout` fails the
whole operation on one non-matching pathspec. Tree-filtering is the right answer for `checkout` — which
this gate no longer needs, because the restore is `-- .` — and carried over to `clean` it only removes
coverage. So `test_roots.json` is a fixed emit-time list with no `ls-tree` gate:

- conventional test roots — `tests/`, `test/`, `spec/`, `t/`, `__tests__/`
- the root config surface — `conftest.py`, `pytest.ini`, `tox.ini`, `setup.cfg`, `jest.config.*`
- the recursive hook surface — `:(glob)**/conftest.py`, so a hook planted in `src/` or a subpackage is
  cleaned too, not only one at the root
- **exclusions for the agent's own install directories** — `:(exclude,glob).venv/**`,
  `:(exclude,glob)node_modules/**`, `:(exclude,glob)**/site-packages/**`

**The exclusions are not tidiness; without them the recursive glob deletes part of the solve.**
`git clean` here runs without `-x`, so an *ignored* `.venv` is already safe — but plenty of repos do not
ignore `.venv`, and for those the glob reaches straight into it. Verified: in a repo with no `.gitignore`
entry for it, `git clean -fdq -- conftest.py ':(glob)**/conftest.py'` removes
`.venv/lib/python3.12/site-packages/<pkg>/conftest.py` and `node_modules/<pkg>/conftest.py`; real
packages do ship those (this repo's own venv has one). And `presets.py:53-55` explicitly instructs the
bootstrap agent to build `/workspace/.venv` when a venv is unavoidable, so an in-workspace venv is a
documented outcome, not a corner case. With the three exclusions the same command leaves both intact
(verified), and exclusions of absent directories still exit 0 (verified), so they cost nothing on the
common path.

The list is therefore never empty. The `${#R2E_ROOTS[@]}` guard stays as cheap defense against a
malformed file, but it is **not sufficient on its own**: a list containing *only* exclusions means
"everything except these" and would clean the entire untracked surface (verified — with a lone
`:(exclude,glob).venv/**`, `node_modules/` was deleted). The emitter must always write at least one
positive pathspec; §10 asserts that shape.

**Honest limits.** `git clean` is scoped to the test + config surface rather than run over the whole
tree, because `clean -fdx -- .` would delete the agent's own `.venv` / `node_modules` — i.e. the solve.
The install-directory exclusions above are the same trade made explicit: they are what keep the
recursive glob from eating the solve, and the price is that a hook planted *inside* an install
directory (a `sitecustomize.py` on the venv's path, a `conftest.py` under `site-packages`) survives the
restore. That is a deliberate exchange, not an oversight — we would rather leave one obscure hiding
place open than break every legitimate venv-based solve. With egress open and the container under the
agent's control there is no version of this gate that closes it anyway, and gate 0 makes the same
admission: we block the shortcut an agent stumbles into, not an adversary who works at it.

**The cost of restoring the whole tracked tree, stated.** Legitimate setup work sometimes edits tracked
files. Most such edits are harmless here — pin a dep in `pyproject.toml`, drop a `use_2to3` from
`setup.py`, then install; the install has already happened by the time gate ½ runs, so restoring the
manifest changes nothing. What does die is the subset whose effect is read **at test time**: pytest
configuration (`[tool.pytest.ini_options]`, `tox.ini`, `setup.cfg`, `pytest.ini`, `conftest.py`) and,
for Go and Rust, tracked source files — `go test` / `cargo test` compile `/workspace` during the
verifier phase. Those solves score 0 on a graded task.

We take that cost knowingly rather than narrowing the restore, because the pytest config surface is
exactly where grading gets gamed (`addopts = -k "not broken"`, a `collect_ignore` in `conftest.py`) and
because Go/Rust F2P ids carry no file to narrow *to*. What we do not do is take it silently: §6's
`instruction.md` states the contract in one sentence, and §5's distillation prompt plus the
tracked-tree assertion in step F keep the oracle inside it. F′ would catch an offending oracle on its
own (§4), but only as an opaque `gates_unverified` at the end of the run; the step-F assertion is what
names the fault and feeds a retry that can fix it.

**Fails closed**, mirroring `pr_runtime`'s `apply_guard` (`pr_runtime.py:481-496`): if the restore
cannot run, reward is 0 and the script exits 0, rather than grading against whatever the agent left
behind. Works whether or not `scrub_git_history` is on — both keep `base_commit` reachable (§6).

`mapfile` rather than `$(cat …)` because unquoted command substitution word-splits on whitespace, and
a path with a space in it would silently become two nonexistent paths.

### Gate 1 — the graded reward

```bash
set +x                                          # keep xtrace out of the parsed log
( <normalized test_cmds joined with &&> ) > /logs/verifier/test_output.log 2>&1
TEST_EXIT_CODE=$?
set -x
cat /logs/verifier/test_output.log

python3 "$SCRIPT_DIR/verifier.py" \
    --log /logs/verifier/test_output.log \
    --f2p "$SCRIPT_DIR/f2p.json" --p2p "$SCRIPT_DIR/p2p.json" \
    --runner '<runner>' \
    --test-cmds '<normalized test_cmds, single-quote-escaped>' \
    --exit-code "$TEST_EXIT_CODE" \
    --out-dir /logs/verifier \
  || { [ "$TEST_EXIT_CODE" -eq 0 ] && write_reward 1.0 verifier_crashed \
                                   || write_reward 0.0 verifier_crashed; }
exit 0
```

The `set +x` / `set -x` bracket is the run-time half of the capture-shape contract from §4: the
subshell's stderr is redirected into the file the parser reads, so with xtrace on, `+ cmd` lines land
in the log and corrupt jest ids. Generation-time step F captures identically.

`reward = f2p_rate` (empty P2P ⇒ `p2p_rate = 1.0`). `resolved` is the strict SWE-bench bool: all F2P
pass. Half-solving scores half, which is the whole point — this pipeline explicitly rejects
exit-code-only grading, and the shared verifier gives graded reward for free. Note the verifier's own
fallback is conservative in the right direction: when the log is unparseable it sets
`resolved = (exit_code == 0) and not has_oracle`, so it refuses to claim `resolved` whenever an F2P
oracle exists, and reports `eval_trustworthy: false`.

`parse_status` values in play: `ok` and `fallback_exitcode` from the verifier, plus
`package_not_from_source`, `provenance_unreadable`, `test_restore_failed`, and `verifier_crashed`
written by the gates above. `verifier_crashed` is distinct from the verifier's own
`fallback_exitcode` on purpose — one means the verifier never completed, the other means it completed
and could not parse the log, and a dataset audit wants to tell those apart.

---

## 8. Anti-contamination posture

| Guard | State | Why |
|---|---|---|
| Egress guard | **Off, and not an option** | The task is unsolvable without network package installs. Neither `_env_guard.egress_guard_compose` (v1) nor `egress_firewall_compose` (v2, used by `pr_to_env`) is called. |
| Git-history scrub | **Off by default** | `CONTRIBUTING.md` and `ci.yml` *are* the intended solve context. Available via `scrub_git_history=True`. |
| Provenance gate | **On by default** | The one real hole (§7). |
| Tamper restore | **On, always** | The F2P set is baked, so the graded test bodies must be too (§7, gate ½). The one guard the prompt *does* mention — one sentence, disclosing the contract, not the reward (§6). |

`env_setup` is the only pipeline in the set that ships with egress open. That is inherent to the task
shape, not an oversight, and the provenance gate buys back the enforcement the network guard would
otherwise provide.

It is also the only pipeline that states one of its guards in the prompt. *The environment enforces, the
prompt never asks* still holds for everything that could leak the answer — the F2P set, the probe, the
recipe. The tamper restore is different in kind: it removes a class of otherwise-reasonable solve from
the board, so leaving it unsaid makes the task unfair rather than uncontaminated (§6, §7). Recorded here
as a deliberate exception so it reads as a decision and not as drift. For context on the rest of the set: `pr_runtime`, `commit_runtime`, and
`cve_patches` ship the scrub plus the v1 compose guard; `pr_to_env` ships the scrub plus the v2
in-container firewall; `pr_diff` ships the scrub only; `code_instruct` and `equivalence_tests` ship
neither.

**Two leak paths the original RFC listed, resolved.** A registry serving a prebuilt image for the
repo is not reachable here — Harbor builds `environment/Dockerfile`, whose `FROM` we write, and the
agent never supplies a base image; the draft's "reject unknown registries in the verifier" check has
no place to run and is dropped rather than left as an unimplemented promise. Package-manager caches
containing pinned versions are not a leak — package installs are the whole point.

---

## 9. Failure modes and skip reasons

| Reason | Trigger | Behavior |
|---|---|---|
| `bootstrap_failed` | `ensure_bootstrap` raises (`BootstrapError` or `DockerError`), or returns `smoke_passed=False`, or `verify_passed=False` | Skip ref, continue |
| `bootstrap_source_unsupported` | `bootstrap.extra["source"] == "user_dockerfile"` — no known bare base to distil against (§4) | Skip ref |
| `base_image_mismatch` | `base_image_for(bootstrap.language)` disagrees with `dockerfile_reconstruction`'s `FROM` (§1) | Skip ref |
| `no_runnable_test_cmds` | Normalized list is empty after dropping blank commands | Skip ref |
| `no_recipe_source` | No transcript **and** no `dockerfile_reconstruction`/`rebuild_cmds` | Skip ref |
| `recipe_unverified` | `max_recipe_attempts` exhausted on a red suite | Skip ref, dump attempts to `.debug_skips/` |
| `recipe_edits_tracked_files` | Suite green, but `git diff --quiet HEAD --` failed after `setup.sh` on every attempt — gate ½ would zero the oracle (§5, §7) | Skip ref, dump attempts to `.debug_skips/` |
| `runner_undetectable` | `detect_runner(" ".join(cmds))` returns `unknown`, so no runner can be baked as `--runner` (§2) | Skip ref |
| `too_few_tests` | `len(target) < min_target_tests` | Skip ref |
| `gates_unverified` | F′ can't reach reward 1.0 — either the probe ladder reached `none` and still failed, or a gate-½/gate-1 failure occurred (those do **not** degrade, §4) | Skip ref |
| `oracle_gate_failed` | `harbor run -a oracle` returned `!= 1.0` | Delete the emitted dir, count as skipped |
| `build_failed` | Any other exception in `_build_task` | Log + skip, never halt |

A failure on the *initial* repo resolution raises; per-ref failures accumulate in `skip_reasons` and
never halt the run. `emitted == 0` returns a `PipelineResult`, it does not raise — the CLI exits 1 on
its own.

A `file://` source is also absent from this table on purpose: it is not a per-ref skip but a pre-flight
rejection (`cli.py:120` ⇒ exit 2) via `Capability.REMOTE_CLONE`, plus the mirroring `__init__` check for
non-CLI callers (§2). Nothing is generated, so there is nothing to count as skipped.

The one asymmetry worth stating plainly: a **raising** bootstrap failure on the **primary** ref never
reaches this table. `cmd_generate` bootstraps that ref before the pipeline is constructed and returns
`1` on `BootstrapError` (`cli.py:224-229`), so the run ends there. Non-raising failures are different:
nothing in `cmd_generate` inspects `smoke_passed`, `verify_passed`, or `extra["source"]`, so the primary
ref reaches the pipeline with a `BootstrapResult` in hand and is gated there like any other — a primary
ref *can* produce `bootstrap_failed`, `bootstrap_source_unsupported`, or `base_image_mismatch` entries.
What it cannot produce is a skip for the cases `ensure_bootstrap` raises on.

---

## 10. Test plan

### Unit — no Docker, no network, no LLM

| Test | Asserts |
|---|---|
| `test_env_setup_options_strict` | `EnvSetupOptions(unknown=1)` raises |
| `test_env_setup_options_target_bounds` | `min_target_tests=0` raises, `max_target_tests=-1` raises, `max_target_tests=0` does not — and `min_target_tests=5, max_target_tests=3` **constructs**: floor and cap are independent (§3) |
| `test_oracle_timeout_covers_inner_budgets` | Default `oracle_timeout_sec=0` derives `max_setup_time_sec + verifier_timeout_sec + oracle_build_slack_sec`; an explicit value below that sum raises (§3) |
| `test_env_setup_rejects_local_source` | A `file://` repo raises in `__init__`, and `required_capabilities` contains `REMOTE_CLONE` so the `cli.py` pre-flight rejects it too (§2) |
| `test_authed_clone_url_handles_gitlab` | `github.com` ⇒ `x-access-token:${ARG}@`, `gitlab.com` ⇒ `oauth2:${ARG}@`; `pr_diff`'s emitted Dockerfile is unchanged for github.com (§6) |
| `test_lang_module_has_no_base_image_literals` | `_env_setup_lang.py`'s source contains no image literal (`python:`, `node:`, `golang:`, `rust:`, `eclipse-temurin:`, `ubuntu:`) — the base comes from `base_image_for` (§1, §2) |
| `test_bare_base_image_honors_spec_override` | `bare_base_image(BootstrapSpec(base_image="alpine:3.20"), PYTHON)` returns `alpine:3.20`, not `python:3.12-slim`, so a `--base-image` run does **not** skip every ref as `base_image_mismatch` (§1) |
| `test_recipe_parse_from_transcript` | Fenced block extracted; non-script prose rejected |
| `test_recipe_retry_carries_stderr` | Second-attempt prompt contains both the failed script and the captured stderr |
| `test_recipe_source_falls_back_to_reconstruction` | Absent `transcript_path` ⇒ `dockerfile_reconstruction` + `rebuild_cmds` feed the prompt; absent both ⇒ `no_recipe_source` |
| `test_user_dockerfile_bootstrap_is_skipped` | `bootstrap.extra["source"] == "user_dockerfile"` ⇒ `bootstrap_source_unsupported`, asserted **without** relying on the `verify_passed=False` default (§4) |
| `test_base_image_mismatch_skips` | A `dockerfile_reconstruction` whose `FROM` differs from `base_image_for(language)` ⇒ `base_image_mismatch`, not an `AssertionError`/`build_failed` (§1) |
| `test_recipe_rejected_when_it_edits_tracked_files` | A distilled recipe that writes a tracked file is retried with that complaint and, on exhaustion, skips `recipe_edits_tracked_files` (§5) |
| `test_instruction_states_tracked_file_contract` | `instruction.md` contains the one restore sentence and still names no test, package manager, or probe (§6) |
| `test_reward_kinds_are_spec_defined` | The `repo2env` dict `_build_task` produces has `reward_kinds == ["test_execution"]` (⊆ the two kinds `SPEC.md:205` defines) and `reward_granularity == "graded"`. Asserted on the built metadata, **not** as a sweep over `PIPELINES` — no pipeline exposes `reward_kinds` as a class attribute (§2) |
| `test_blank_test_cmds_dropped` | `["| head -50", "2>&1", "  "]` normalizes to `[]` ⇒ `no_runnable_test_cmds`, never a `" && "`-joined empty segment |
| `test_dockerfile_has_no_installs` | No `pip install` / `npm install` / `cargo build` / `go mod download` — the regression guard for the single mistake that silently voids the pipeline |
| `test_dockerfile_has_no_sentinel_or_commit` | No sentinel injection and no `git commit` line (a `git commit` with a clean index exits 1 and would break the build for every non-Python language) |
| `test_provenance_probe_python_direct_url` | On the `direct_url` rung: passes for a `direct_url.json` pointing at `file:///workspace`; passes for import-path-under-`/workspace` with no dist metadata; **fails** for a site-packages import with no `direct_url.json`; a missing `dist_name` key does not raise (§7) |
| `test_provenance_probe_path_rung_ignores_dist_metadata` | `provenance.py` honors the rung: with `probe="path"`, a dist carrying `direct_url.json` but importing from site-packages **fails**, where the same input on `direct_url` passes. Guards against the rung being read but ignored (§7) |
| `test_probe_degrades_in_one_step_to_none` | The degradation for a failing Python probe is `direct_url` → `none`, **never** `direct_url` → `path`: `path`'s check is a subset of `direct_url`'s, so an intermediate rung could only fail identically (§7) |
| `test_provenance_probe_node_rejects_node_modules` | A resolution under `/workspace/node_modules` fails |
| `test_provenance_read_contract` | `provenance_read.py` emits exactly three lines in order (probe, base_commit, language); exits non-zero on malformed JSON, a missing key, a blank value, and a value containing a newline (§7) |
| `test_gate0_fails_closed` | Unreadable/absent `provenance.json` ⇒ reward 0.0 with `parse_status="provenance_unreadable"`, not a silent pass |
| `test_gate0_passes_probe_and_language_as_args` | `provenance_run.sh` is invoked with `"$R2E_LANG" "$R2E_PROBE"` and contains **no** `python3 -c` read of `provenance.json` — one read of that file per script (§7) |
| `test_test_sh_gate0_golden` | Golden-file comparison per probe kind (`direct_url` / `path` / `none`) — three genuinely distinct emitted shapes, which only holds because `provenance.py` branches on the rung (§7) |
| `test_test_sh_emits_path_prelude` | The emitted `test.sh` carries `_path_prelude_for_language(language)` for a `go`/`rust`/`node` task, so the runner binary is on `$PATH` in Harbor's non-interactive shell (§7) |
| `test_runner_undetectable_skips` | A `test_cmds` list whose joined form yields `detect_runner(...) == "unknown"` ⇒ `runner_undetectable`, never a baked empty `--runner` and never `too_few_tests` (§2, §9) |
| `test_too_few_tests_skips` | 3 passing tests with `min_target_tests=5` ⇒ `too_few_tests` |
| `test_gates_unverified_when_probe_ladder_exhausts` | A probe that fails at every rung including `none` ⇒ `gates_unverified`, and no task directory is left behind |
| `test_test_sh_passes_runner_and_exit_code` | The emitted `test.sh` contains `--runner`, `--test-cmds`, `--exit-code`, and `--out-dir` (the name is accurate now — the draft's version asserted only `--test-cmds`) |
| `test_test_sh_disables_xtrace_around_capture` | `set +x` precedes the redirected subshell and `set -x` follows it |
| `test_gate_half_restores_whole_tree` | The emitted script uses `git checkout <base_commit> -- .`, never a computed multi-pathspec list, and never a bare `--` |
| `test_test_roots_include_unmatched_config_surface` | The list is **not** tree-filtered: a repo with no `conftest.py` at `base_commit` still emits `conftest.py` and `:(glob)**/conftest.py`, so an agent-added hook is cleaned; the list is never empty (§7) |
| `test_test_roots_exclude_install_dirs` | The list contains `:(exclude,glob).venv/**`, `:(exclude,glob)node_modules/**`, and `:(exclude,glob)**/site-packages/**`, **and** at least one positive pathspec — an exclude-only list would clean the whole untracked tree (§7) |
| `test_normalized_cmds_used_everywhere` | `metadata.test_cmd` equals the command string embedded in `test.sh` |
| `test_env_prelude_from_test_cmds` | `. /workspace/.venv/bin/activate && pytest -v` yields `. /workspace/.venv/bin/activate` with **no trailing `&&`**; a bare `pytest -v` yields `true`; a fragment containing `'` round-trips through the shipped file unmangled |
| `test_target_set_uses_verifier_parser` | The F2P set comes from `_pr_runtime_verifier.parse_logs` (not a local reimplementation); `SKIPPED` never enters `f2p.json` |
| `test_target_floor_applied_before_truncation` | `min_target_tests=5`, `max_target_tests=3`, 10 passing tests ⇒ emitted with 3 F2P, not skipped — a legal config now that the floor/cap validator is gone (§3), which is what makes the ordering observable at all |
| `test_emitter_omits_solution_without_oracle` | `HarborTask(oracle_diff=None)` writes no `solution/` and `_content_hash` does not raise |
| `test_emitter_timeouts_are_per_task` | `agent_timeout_sec` / `verifier_timeout_sec` reach `task.toml`; `None` reproduces today's `1800.0` / `300.0` |
| `test_public_base_covers_every_language` | Every base `base_image_for` can return satisfies `_is_public_base_image` (§14) |
| `test_pipeline_contract` | Picks the new entry up automatically |

### E2E — `skipif` no Docker / no `gh`

`pallets/click` at HEAD, `limit=1`: assert `emitted >= 1`, the emitted Dockerfile contains no install
commands, `f2p.json` is non-empty, and `harbor run -a oracle` returns `1.0`.

Three behavioral assertions that only a container can make:

- **Gate ½ cleans an added hook — measured in a container where it would otherwise help.** Asserting
  "reward ≠ 1.0" in the *oracle-green* container proves nothing: gate ½ removes the hook, the suite
  genuinely passes, and the reward is 1.0 either way. So run it in a container where `setup.sh` was
  deliberately **not** run — the suite is red — then plant `/workspace/conftest.py` with a
  `pytest_runtest_makereport` forcing every outcome to `passed`. Without gate ½ that scores 1.0; with it,
  0.0. The hook has to be able to change the answer for the test to be measuring anything.
- **Gate ½ does not eat the solve.** In the oracle-green container, install into `/workspace/.venv` in a
  repo that does not gitignore it, confirm some installed package ships a `conftest.py`, run
  `tests/test.sh`, and assert reward `== 1.0` and that the package's `conftest.py` still exists. This is
  the regression guard for the install-directory exclusions (§7).
- **`git clean` tolerates the pathspecs we ship.** Run the emitted gate ½ on a repo where most of
  `test_roots.json` matches nothing and assert exit 0 — the property the unfiltered list depends on.

The full suite must stay green (`./.venv/bin/python -m pytest -q`; note `uv run pytest` resolves the
wrong interpreter in some checkouts).

---

## 11. Yield & repo suitability

- **Expected yield** — high. Any repo where `bootstrap/` produces a working image is a candidate,
  which is already most Python / Node / Go / Rust libs we've tested. The binding filters are
  `min_target_tests` and the recipe-verification retry loop, not discovery.
- **What repos work?** — CPU-only suites, no exotic system deps. Python / JS / Go / Rust — polyglot
  from day one, unlike `code_instruct` / `equivalence_tests`.
- **What repos don't work?** — GPUs, external services (a live database, a paid API), or system-level
  setup outside a container (kernel modules, systemd, sudo).
- **How much history?** — one env per `(repo, ref)`. Multiple refs per repo is possible but each ref
  costs a full bootstrap (§4, §15).

---

## 12. Dependencies

- **Reused** — `bootstrap/` (the whole subsystem — that's the point), `_pr_runtime_verifier.py`
  (F2P/P2P grading, now with `detect_runner` public), `_eval_script.py` (`make_unified_diff`, the
  relocated `normalize_test_cmds_for_runtime` and `_path_prelude_for_language` — the latter emitted into
  `test.sh` exactly as `pr_runtime` does it, §7 — plus the new `env_prelude_from_test_cmds` and
  `authed_clone_url`. `authed_clone_url` is shared with `pr_diff`, which adopts it to fix its live
  private-GitLab bug (§2, §6); `env_prelude_from_test_cmds` has no second consumer yet and is
  `env_setup`-only for now), `sources.py` (`Capability.REMOTE_CLONE`, new), `_env_guard.py`
  (git-history scrub only, and only when opted in).
- **Not reused** — `_eval_script.build_binary_eval_script`. The shared verifier already has an
  exit-code fallback (`parse_status="fallback_exitcode"`); two fallback paths is one too many.
- **New external deps** — none. Everything is stdlib + existing internals.

---

## 13. Alternatives considered

- **Bake the oracle Dockerfile into the emitted task, agent edits it.** Rejected: the whole point is
  that the agent starts with *nothing*. Watering that down turns it into "modify a Dockerfile," a much
  smaller task.
- **Grade on `pytest` exit code only.** Rejected: too coarse. An agent that gets 40 of 50 tests
  passing should score 0.8, not 0.0.
- **Python-first instead of multi-language.** Rejected: `bootstrap/` already auto-detects language,
  the F2P shape doesn't care what language the tests are in, and being polyglot from v1
  differentiates this from Repo2Run's Python-only scope.
- **A sentinel attribute for provenance.** Rejected in favor of PEP 610 metadata (§7): the sentinel
  is forgeable by `grep` + a two-line append, requires a package-root heuristic and an in-tree commit,
  and still rejects `pip install .` on the path branch.
- **Per-file test restore driven by F2P ids.** Rejected in favor of restoring the whole tracked tree
  (§7): the id-derived path list is empty for Go and Rust, and its conventional-roots fallback zeroes
  every task that uses it. **The option we chose has a cost, recorded here rather than only in the
  option we rejected:** restoring everything also reverts tracked-file edits that are legitimate setup
  work, in the subset whose effect is read at test time (pytest config; Go/Rust sources, which compile
  during the verifier phase). Narrowing the restore to the test surface would recover those solves and
  simultaneously reopen the surface where grading is gamed, so we keep the broad restore and disclose
  it in one sentence of `instruction.md` (§6) — an explicit exception to "the environment enforces, the
  prompt never asks", not an oversight.

### Open questions, resolved

1. **Bake target test names, or let the agent's setup decide what runs?** **Bake** (`f2p.json`).
   Deterministic, comparable across attempts, and test-discovery is a secondary skill we are not
   trying to measure. Baking is also what makes gate ½ necessary.
2. **Lockfile vs. non-pinned repos?** Tag it — `has_lockfile` in metadata. No filtering; let the
   consumer rebalance.
3. **Multi-language P2P?** P2P stays empty. The shared verifier maps that to `p2p_rate = 1.0`, so
   there is nothing to build.

---

## 14. Rollout

1. **Smoke** — 3 envs: `pallets/click`, `psf/requests`, `python-attrs/attrs`. Confirm oracle 1.0 and
   `recipe_attempts == 1` for at least one.
2. **Polyglot smoke** — one Node repo and one Go repo, to exercise the `path`/`none` probe split and
   the jest capture shape before scaling. Add one **private GitLab** repo here: it is the only case that
   exercises `authed_clone_url`'s `oauth2@gitlab.com` branch, and it is the case the inherited
   `pr_diff` URL builder silently broke (§6).
3. **Scale** — ~100 envs across ~100 repos, one env per repo at HEAD (§15 keeps multi-ref fan-out out
   of v1, so the env count and the repo count are the same number). Concretely: **one `generate`
   invocation per repo** — `generate` takes a single `--repo`, so there is no multi-repo run and the
   default `limit=20` never binds (§3).

   ```bash
   while read -r repo; do
     repo2rlenv generate --repo "$repo" --pipeline env_setup \
       --llm anthropic/claude-sonnet-4-6 \
       --out "./datasets/env-setup/$(basename "$repo")"
   done < repos.txt
   ```

   Each invocation pays one bootstrap; the running `bootstrap_cost_usd` per task (§6) is the only cost
   record, since `PipelineResult` has no cost field (§4).
4. **Oracle gate** — hard requirement, enforced in-pipeline.
5. **Real-agent eval** — 10 sampled envs via Harbor with claude-code + Sonnet 4.6. Report solve rate
   against Repo2Run's ~55% Python baseline, **and** the gate-0 failure rate — how often a real agent
   takes the PyPI shortcut is the number that tells us whether the provenance gate was worth
   building. Collect `oracle_test_time_sec` across the corpus here and set `verifier_timeout_sec`'s
   default from the observed p99 rather than from a guess — `oracle_timeout_sec` follows automatically,
   since its default is derived from that number (§3).
6. **Publish** — `AdithyaSK/repo2rlenv-env-setup` with `push --mode inline`. **Verify it takes the
   self-contained fast path**, and add a test that asserts it. `env_setup`'s Dockerfile is
   `FROM <public base>` + clone with no upstream bootstrap image, which is exactly the shape
   `registry/integration.py:456` short-circuits via `_finalize_self_contained_tasks` (`:457-459`) — no
   image push, consumers rebuild from the inline recipe.

   Missing that fast path is what would hurt. Past it there is a **three-way** branch, not a single
   fallthrough: `_go_inline` when `inline_dockerfile` is set (`:473-482`), `_go_inline` with a
   `fallback_reason` when no verified registry is available (`:491-513`), and otherwise **`_go_registry`
   (`:516`)**, which is the default. The `_go_inline` arm is the dangerous one: it rewrites every task's
   Dockerfile as *bootstrap recipe + per-task overlay* (`:580-600`), splicing the dependency installs
   back in and **destroying the pipeline's premise**. `_go_registry` is merely wasteful here — it would
   push an image per repo that consumers do not need.

   The fast path is gated on `_is_public_base_image(local_ref)` and `not looks_local`, where
   `local_ref = sorted(distinct)[0]` (`:439`) — **one representative decides for the whole dataset**.
   `eclipse-temurin:` was absent from `_PUBLIC_DOCKER_HUB_BASES` (`:116-126`, which listed `openjdk:`
   instead), and since `"eclipse-temurin:21-jdk"` sorts before every other base we emit, a single Java
   task in a polyglot dataset would have dragged the whole dataset off the fast path. That gap is closed
   at `:125` — **as an uncommitted working-tree change**, so it ships with this pipeline rather than
   ahead of it — and §10's `test_public_base_covers_every_language` is what keeps it closed as
   `base_image_for` grows. The per-representative decision remains: making it per-task is the correct
   fix and is filed for v2 (§15).
7. **Docs** — `docs/pipelines/env_setup.md`, `docs/pipelines/README.md` rows, `mkdocs.yml` nav,
   findings in `plans/env_setup_audit_iter*.md`.
8. **Ship `experimental`.** Promote after a release cycle of real use.

---

## 15. Out of scope for v1

- Multi-ref fan-out beyond an explicit `refs` list. One env per repo at HEAD is the reference dataset;
  "same repo at three points in dependency-drift history" is a v2 idea, and an expensive one — every
  extra ref costs a full bootstrap, and `PipelineResult` has nowhere to report the spend (§4).
- Any Java / C++ provenance probe (`none` in v1).
- **Local (`file://`) repos.** The build-time-clone pattern can't reach a host path from inside a
  Docker build; supporting it means shipping the repo through the build context or a
  `docker cp` + `commit` step. (The RFC's Input section previously claimed local support; it is
  excluded until that exists.) The exclusion is **enforced**, not just documented: `env_setup` requires
  `Capability.REMOTE_CLONE`, so the `cli.py:120` pre-flight rejects a local source with the usual
  "pipelines that work on this source" message, and `__init__` re-checks for non-CLI callers (§2).
  Nothing about this is `env_setup`-specific — `pr_diff` emits the same build-time clone and could adopt
  the capability later; it is currently gated out of local sources by `PULL_REQUESTS` instead.
- Per-task fast-path selection in `registry/integration.py` (§14, step 6).
- Rewarding recipe *quality* — image size, layer count, version pinning, reproducibility. Reward is
  test outcome only. Grading recipe aesthetics invites reward hacking on a dimension we cannot verify.
- GPU / external-service repos, excluded by the same constraints that bound `bootstrap/` today.

---

## Revision history

**2026-08-16 — review pass: code re-verification + design-defect sweep.** Every file:line claim in the
document was re-checked against the working tree, and every claim the RFC marks "verified" was re-run.
The empirical ones all held — `git checkout` failing on one non-matching pathspec, `git clean` exiting 0
on pathspecs that match nothing, a bare `git checkout <sha> --` detaching HEAD, `git commit` exiting 1 on
a clean index, PEP 610 `direct_url.json` present for editable/local installs and absent for PyPI ones,
`packages_distributions()` omitting `.pth` editable installs, and the `set +x` / `TEST_EXIT_CODE=$?`
ordering in gate 1 capturing the subshell status correctly. Six design defects and a set of factual
corrections did not:

| # | Was | Now |
|---|---|---|
| D1 | The probe ladder's first two rungs were the same program: `provenance.py` ran `dist OR import` unconditionally and never read `probe`, so degrading `direct_url`→`path` rewrote a JSON string and re-ran a byte-identical script | Two changes, because fixing the first exposed the second. `provenance.py` now takes the rung as `argv[2]` and branches (`direct_url` = dist-OR-import, `path` = import-only; verified the two now differ on the same input). But that made it plain that `path` is a strict **subset** of `direct_url` and can never pass where it failed — so the three-step ladder is gone too: the probe kind is per-**language** (§7's table) and degradation is one step to `none`. Also: a missing `dist_name` no longer `KeyError`s, and only a missing *import* name forces `none` (§4, §7, §10) |
| D2 | `bare_base_image(lang)` delegated to `base_image_for(lang)` alone, so any `--base-image` run disagreed with the reconstructed `FROM` and skipped **every** ref `base_image_mismatch` — contradicting §1's "they agree by construction" | `bare_base_image(spec, lang)` returns `spec.base_image or base_image_for(lang)`, the same expression `runner.py:547` evaluates (§1, §2, §10) |
| D3 | §4's B′ argued at length that `normalize_test_cmds_for_runtime` is "strictly 1:1" and can emit `""` | It drops blanks itself (`pr_runtime.py:665-666`, `:701-703`, uncommitted); verified empirically. The filter is kept as insurance, and the *index-alignment* assumption is called out as the thing that actually broke (§4) |
| D4 | Gate ½'s `:(glob)**/conftest.py` reached into `/workspace/.venv` and `node_modules/` when they aren't gitignored, deleting `conftest.py` files shipped by installed packages — i.e. part of the solve — while §7's "Honest limits" claimed install-dir hooks survive | Three `:(exclude,glob)` pathspecs for `.venv/**`, `node_modules/**`, `**/site-packages/**`; verified they fix it and cost nothing when absent. The exclude-only-list footgun is documented, and "Honest limits" now states the trade as a consequence of the exclusions (§7, §10) |
| D5 | §7's `test.sh` head claimed to mirror `pr_runtime`'s "with two additions" but silently dropped `_path_prelude_for_language`, which `pr_runtime.py:501` emits to stop `go test`/`cargo test`/`node` exiting 127 in Harbor's non-interactive shell — on a pipeline that is polyglot from v1 and whose toolchain is installed by the agent | The prelude is back in the head, with the two preludes' different jobs spelled out (§7, §10, §12) |
| D6 | §1 listed `reward_kinds` as a pipeline property and §10 asserted it across every registered pipeline; no pipeline declares it — it is a key each `_build_task` writes into `repo2env` | Documented as emitted metadata, and the test rewritten to assert the built dict rather than sweep `PIPELINES`. `reward_granularity` is flagged as undocumented in `SPEC.md`, which now needs two edits, not one (§1, §2, §10) |
| D7 | §4, §5, §7 and §13 all claimed F′ "inherits the same blind spot" and proves nothing about tracked-file edits | False: F′ re-runs `test.sh` without re-running `setup.sh`, so gate ½ reverts the edit and gate 1 goes red. Step F's `git diff --quiet` is reframed as earlier, better-named diagnosis, and the *real* residual — solve shapes unlike the oracle's, where `env_prelude.sh` activates the wrong environment — is stated instead (§4, §5) |
| D8 | §2 and the merge table faulted `ADDING_A_PIPELINE.md` for a `_discover`/`_should_skip` shape | The cookbook has since been corrected and now says "never a generic `_should_skip`" (`:350`); the criticism is removed rather than left attacking a straw man (§2) |
| D9 | §2 argued we "know the runner at emit time" while §4 derived it with the identical heuristic; an `unknown` result had no skip reason and would surface as `too_few_tests` | The claim is narrowed to *resolve once, where `unknown` is observable*, and `runner_undetectable` is a first-class skip reason in the flowchart, §9, and §10 |
| D10 | "One read, and it fails closed" was false — `provenance_run.sh` did its own `python3 -c` read of the same file with a different failure posture | `provenance_read.py` emits probe + base_commit + language; the language and probe are passed to `provenance_run.sh` as arguments. One read per file, and the two-files-one-read-each shape is stated (§7) |
| D11 | The E2E gate-½ assertion planted a `passed`-forcing hook in the oracle-**green** container and asserted reward ≠ 1.0 — unachievable, since the suite passes either way and the hook cannot change the answer | Run in a red container (setup deliberately skipped) where the hook *would* score 1.0 without the gate; plus a new assertion that gate ½ does **not** delete an in-workspace venv's `conftest.py` (§10) |
| D12 | `provenance_read.py` was the only emitted file with no specified body, though gate 0's fail-closed contract depends on its exact output shape | Body specified, including the blank/newline validation that stops a desynced three-line read (§7, §10) |
| D13 | "(`cli.py:832` prints it, no pipeline reads it)" — `:832` prints `smoke_passed`; `verify_passed` is never surfaced anywhere. §2's `BootstrapError` list also missed two raise sites | Corrected, and the `verify_passed` vacuity hole added: `_verify_committed_image` returns `True` without executing anything when `test_cmds` is empty (`runner.py:269-270`), so B′ — not the bootstrap gate — is what catches that case (§4) |
| D14 | `write_reward`'s default `parse_status` was `fallback_exitcode`, the same string `verifier.py` writes on an unparseable log, making "verifier died" and "verifier degraded" indistinguishable; the helper was also described as having "three call sites" | Default is `verifier_crashed`, the gate-1 fallback passes it explicitly, and the count is seven across three gates (§7) |
| D15 | `provenance_gate=False` was never specified, and the emitted gate 0 branches only on the baked probe — so the option was silently inert | `False` bakes `probe="none"` and skips the F′ ladder (§3) |
| D16 | §3's options snippet used `model_validator` and a `@property`; `spec/options.py` has neither anywhere and enforces cross-field rules in pipeline `__init__`s | Stated as a deliberate first, with the added pydantic import called out (§3) |
| D17 | "If it instead falls through to `_go_inline` (`:533`)" — past the fast path there is a three-way branch whose *default* is `_go_registry` (`:516`) | Corrected, with `_go_inline`'s two entry conditions distinguished from the wasteful-but-safe registry default (§14) |
| D18 | The `eclipse-temurin:` allowlist entry and the blank-dropping `normalize` were both described as already-closed facts | Both are **uncommitted working-tree changes**; they ship with this pipeline rather than ahead of it (§2, §4, §14) |
| D19 | "`docs/pipelines/README.md` (three tables)" and "`AUTH.md` (the `GIT_TOKEN` build arg)" | Four pipeline tables plus the `:22` count sentence; `AUTH.md:68-77` is `pr_diff`-scoped prose documenting no GitLab form at all and needs rewriting; eight places state a pipeline count, two already stale from `pr_to_env`. Itemized in §2 |
| D20 | §9 said `bootstrap_failed` on the primary ref "never reaches this table" and scoped `gates_unverified` to probe-ladder exhaustion only | Only *raising* failures bypass the table — non-raising ones are gated in-pipeline like any other ref; `gates_unverified` also covers non-degrading gate-½/gate-1 failures (§9) |

**2026-08-16 — review pass: internal-contradiction sweep.** A read of the merged document against the
code turned up nine places where the RFC contradicted itself, specified something unreachable, or left
the cost of a chosen option unrecorded. Resolutions:

| # | Was | Now |
|---|---|---|
| A1 | `_check_target_bounds` rejected `max_target_tests < min_target_tests`, which §10's floor-before-truncate test requires — and, since `max ≥ min` makes `min(len, max) ≥ min` automatic, no legal config could observe the ordering §4 argues for | Validator replaced by the invariant `grade()` actually needs (`min_target_tests ≥ 1`, `max_target_tests ≥ 0`); floor = corpus admission, cap = grading budget, deliberately independent (§3, §4) |
| A2 | §5's fallback #2 justified by `spec.user_dockerfile`, a path §4's `verify_passed` gate already rejects — by an incidental field default (`spec.py:44`), and one that would also break §1's `FROM` assertion | That bootstrap source is rejected **by name** (`bootstrap_source_unsupported`, §4); fallback #2 keeps only its real justification (pre-transcript cache entries); the `FROM` disagreement became a skip (`base_image_mismatch`) instead of an `assert` (§1, §9) |
| A3 | Gate ½ filtered `test_roots.json` against `git ls-tree`, so the added `conftest.py` in its own motivating threat was never emitted and never cleaned | List emitted unfiltered — conventional roots + config surface + `:(glob)**/conftest.py`; the `ls-tree` filter belonged to `checkout`, which the gate no longer uses. Residual limits (hooks inside install dirs) stated (§7, §10) |
| A4 | `_env_setup_lang.py` owned "bare base image", a third literal table after the one §1 exists to prevent | Module delegates to `base_image_for` and holds no image literals (asserted in §10); §7's column marked illustrative with the code as source of truth |
| A5 | `oracle_timeout_sec=1800` bounded a `harbor run` whose inner budgets already summed to 3600 s — a task using its stated budget could not pass its own gate | Default `0` ⇒ derived as `max_setup_time_sec + verifier_timeout_sec + oracle_build_slack_sec`, with the invariant stated and validated (§3) |
| A6 | `required_capabilities = frozenset()` waved a `file://` repo into a Docker build that can't reach it; GitLab was "claimed" while the inherited clone builder dropped the token | New `Capability.REMOTE_CLONE` (github/gitlab, not local) enforces §15's exclusion in the pre-flight, with an `__init__` re-check; GitLab is **in v1** and the shared `authed_clone_url` moved from out-of-scope into §2/§6 — it also fixes `pr_diff`'s live private-GitLab bug (§2, §6, §15) |
| C1 | Whole-tree restore's cost was recorded nowhere: only the *rejected* per-file variant carried a rationale, and F′ proves nothing here because it replays the oracle's own shape | Cost stated in §7 and §13 (the dying subset is edits read *at test time* — pytest config; Go/Rust sources); one disclosed sentence in `instruction.md` (§6) as an explicit exception to "the environment enforces, the prompt never asks"; step F now asserts the recipe leaves the tracked tree clean, with `recipe_edits_tracked_files` as its own skip reason (§5, §9) |
| C2 | `reward_kinds = ["test_execution", "graded"]` invented a kind `docs/reference/SPEC.md:205` doesn't define | `["test_execution"]`; gradedness recorded as `reward_granularity` in metadata, SPEC.md added to the docs-touched list, and §10 asserts every pipeline's kinds are spec-defined (§1, §6) |
| C3 | "every *additional* ref" was undefined against `repo.ref`, so a `refs` list omitting it silently paid for an unused bootstrap; `limit`'s inertness and the 100-invocation scale plan were unstated | Candidate set defined as `[repo.ref] + refs`, deduped on resolved SHA; `limit` semantics spelled out; §14 step 3 shows the one-invocation-per-repo loop (§3, §4, §14) |

**2026-08-16 — merged `0008-env-setup-design.md` into this RFC.** The design spec existed as a
separate companion document with a "deltas from the RFC" table; keeping *why* and *how* in two files
meant the two disagreed in eleven places that the delta table never listed, and the RFC's own
`instruction.md` text had gone actively wrong. The substantive corrections folded in during the merge:

| Area | Was | Now |
|---|---|---|
| Emitter | "two additive changes" | Four (§2). `oracle_diff = None` in place is a `TypeError` at import; `agent`/`verifier` timeouts are hardcoded literals with no override path |
| Instruction text | "Write `environment/Dockerfile` … when Harbor rebuilds and runs `pytest`" | Harbor never rebuilds an agent-edited Dockerfile; the agent sets up the live container (§6) |
| Provenance | Committed `__r2e_setup_sentinel__` + package-root heuristic + baseline commit | PEP 610 `direct_url.json` OR workspace import path (§7) — also deletes the injector, the heuristic, a skip reason, and a build-breaking empty `git commit` |
| Gate ½ | `git checkout <sha> -- tests/ test/ spec/ t/` from F2P-derived paths | `git checkout <sha> -- .` + `git clean` of a fixed emit-time test-root and config-surface list, **not** tree-filtered (§7, and A3 below); the old form fails closed on every Go/Rust task and leaves `conftest.py` open |
| Prelude | `export R2E_ENV_PRELUDE='…'` + `eval`, fragments keeping their trailing `&&` | Shipped `tests/env_prelude.sh`, sourced (§7) |
| Gate 0 | Read with no error handling — failed **open** | One read, fails closed with `provenance_unreadable` (§7) |
| Log capture | "cannot diverge by construction" | `set +x` around the graded run; jest ids verifiably corrupt otherwise (§4, §7) |
| Eval-only split | `use_oracle_recipe=False` "skips distillation entirely" | `emit_solution=False` — distillation still runs; there is no other route to the F2P set (§5) |
| `test_cmds` | Empty-list check after normalization | Blank-command filter; normalization is 1:1 and can emit `""` (§4) |
| Bootstrap gate | `smoke_passed` | `smoke_passed` **and** `verify_passed` (§4) |
| Base image | "from `presets.PRESETS`" | `base_image_for()`; `runner.py` never reads `PRESETS` (§1) |
| Language override | `--force-language` | `--language`; `--force-language` is a compat-check bypass (§3) |
| Verifier args | `--test-cmds` only | `--runner` too (§2) |
| Pipeline shape | `_discover` → `_should_skip` per the cookbook | The real convention: fat `run()` plus domain-named filters (§2). *Superseded by D8 in the pass above — the cookbook has since been corrected and no longer prescribes the wrong shape.* |
| Publish | "`push --mode inline`" | Same, but only via the self-contained fast path — with the `_go_inline` hazard and the `eclipse-temurin` gap called out (§14) |
| P2P / target tests | `p2p.json` "names + statuses"; `target_tests: list[str]` in TOML | `[]`; `target_test_count` (§4, §6) |
| Node base | `node:20-slim` | `node:22-slim` (§7) |
| SWE-bench parity | Implied | Explicitly lost — applying `patch.diff` alone scores 0 (§6) |

---

## References

- Repo2Run: [arXiv:2502.13681](https://arxiv.org/abs/2502.13681) — [bytedance/Repo2Run](https://github.com/bytedance/Repo2Run) (Python-only, eval-focused)
- SetupBench: [arXiv:2507.09063](https://arxiv.org/abs/2507.09063) — [microsoft/SetupBench](https://github.com/microsoft/SetupBench) (multi-language, pass/fail scoring)
- EnvBench: [arXiv:2503.14443](https://arxiv.org/abs/2503.14443) — [JetBrains-Research/EnvBench](https://github.com/JetBrains-Research/EnvBench) (multi-language, canonical-smoke scoring)
- SWE-bench (F2P/P2P semantics we reuse): [arXiv:2310.06770](https://arxiv.org/abs/2310.06770)
- PEP 610 — *Recording the Direct URL Origin of installed distributions*: [peps.python.org/pep-0610](https://peps.python.org/pep-0610/) (the provenance gate's foundation)
- In-repo prior art: `src/repo2rlenv/bootstrap/` (agent primitives we're exposing),
  `src/repo2rlenv/pipelines/_pr_runtime_verifier.py` (verifier we reuse),
  `src/repo2rlenv/pipelines/pr_runtime.py` (F2P/P2P task shape we mirror),
  `src/repo2rlenv/pipelines/pr_diff.py` (build-time-clone Dockerfile pattern).

## Implementation

| | |
|---|---|
| **Initial PR** | _(pending)_ |
| **Shipping release** | _(pending — target v0.9.0)_ |
| **Source file** | [`src/repo2rlenv/pipelines/env_setup.py`](https://github.com/huggingface/Repo2RLEnv/blob/main/src/repo2rlenv/pipelines/env_setup.py) |
| **Options model** | [`src/repo2rlenv/spec/options.py`](https://github.com/huggingface/Repo2RLEnv/blob/main/src/repo2rlenv/spec/options.py) — `EnvSetupOptions` |
| **Doc page** | [`docs/pipelines/env_setup.md`](../pipelines/env_setup.md) |
| **Findings / release notes** | _(pending)_ |
| **Reference dataset** | [`AdithyaSK/repo2rlenv-env-setup`](https://huggingface.co/datasets/AdithyaSK/repo2rlenv-env-setup) *(pending)* |
| **Follow-up PRs** | _(pending)_ |
