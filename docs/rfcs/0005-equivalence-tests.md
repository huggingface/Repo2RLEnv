# RFC 0005: `equivalence_tests`

**Status:** implemented (experimental) — hardened v0.8.7 (leak fix, retry, gates, purity + importability filters)
**Author:** `@adithya-s-k`
**Created:** 2026-05-01 *(retrospective — pipeline shipped in v0.7.0; RFC written 2026-07-15 as archival record; updated 2026-07-22 for v0.8.7 self-improvement pass)*

## Summary

R2E-style function-level synthesis. Extract a real Python function from the target repo, freeze it as an oracle (`reference_<name>`), and ask the LLM to write **equivalence tests** that compare a `<name>` candidate against the oracle on crafted inputs. The gold patch fills in the candidate with the original implementation. Reward: `test_execution` on the LLM-authored equivalence test.

## Motivation

`code_instruct` asks the LLM to invent an entire problem. That's a lot of load on the model — get the problem wrong and the whole task is unsolvable. R2E's insight: **start from real code**. The function you're testing is already real; the LLM only has to write the test that discriminates a working implementation from a broken one. Narrower LLM job → lower failure surface → cleaner tasks.

The counter-argument: it's Python-only and function-level, so the coverage is narrower than `code_instruct`. Our answer: yield per repo is much higher (one task per qualifying function, of which there can be hundreds), and the tasks are cleaner precisely *because* the LLM job is smaller.

## Design

### Input

- **Source** — GitHub · GitLab · local.
- **Trigger** — `repo2rlenv generate --pipeline equivalence_tests --repo <owner>/<name> --pipeline-opt limit=50 --llm anthropic/claude-sonnet-4-6 ...`
- **Options model** — `EquivalenceTestsOptions`: `limit`, `max_attempts_per_function`, `min_loc`, `max_loc`, decorators-to-exclude, side-effect exclusions, generation LLM knobs. Python-only.

### Algorithm

1. Enumerate Python source files; extract candidate functions with the R2E-inspired filter set (LOC bounds, must-have-return, name + decorator + side-effect exclusions).
2. For each qualifying function `<name>`: rename its definition to `reference_<name>` in a fresh working copy (frozen oracle).
3. **LLM writes an equivalence test**: given the original code + the oracle name, generate a pytest that asserts `<name>(inputs) == reference_<name>(inputs)` over a distribution of crafted inputs.
4. **Two-stage gate** in the sandbox:
   - Stage A: test the LLM's test on a stub candidate (function body replaced with `raise NotImplementedError`). Must FAIL.
   - Stage B: test the LLM's test on the original candidate. Must PASS.
5. If both gates pass, emit the Harbor task. Gold patch = the original function body.

### Output

- Task shape: standard sandbox-verified. Test.sh contains the LLM's equivalence test.
- `[metadata.repo2env]` provenance: `equivalence_tests` subtable with `function_name`, seed file path, LOC.

## Verification

- **Reward kind** — `test_execution`. Binary today.
- **Oracle invariant** — the original function body (the gold patch) passes the LLM's equivalence test at emit time. Enforced by Stage B.
- **Non-tamper** — same reset + reapply pattern.

**Known limitation**: reward is binary. Graded port on the v0.9 roadmap.

## v0.8.7 self-improvement pass (2026-07-22)

Baseline audit + smoke on `pallets/click` surfaced two dominant failure modes that made the pipeline effectively unusable end-to-end:

1. **Instruction leak.** `_build_instruction` embedded the full reference source in `instruction.md`, so any solving agent could copy the oracle verbatim. The RFC claimed "signature + docstring only" but the code shipped the whole body.
2. **`oracle_does_not_satisfy_test` at 97% skip rate.** Root cause was NOT weak LLM tests — it was that `task_module.py` failed to *import*. The extractor happily picked functions whose signatures annotated params with repo-internal types (e.g. `def argument(*param_decls: str, cls: type[Argument] | None = None) -> Callable[[FC], FC]`) that don't exist in the standalone stub. The sandboxed pytest never reached the LLM's assertions; it crashed at collection with `NameError: name 'Argument' is not defined`.

Both are pipeline bugs, not model-capability issues. Fixes shipped on the `equivalence-tests-selfimprove` branch:

- **Leak fix** — `_build_instruction` now uses the new `signature_only_source` helper (in `_eval_script.py`) to emit `def name(args): """docstring""" ...` — no body. Instruction cleanly points the agent at `reference_<name>` in the mounted `task_module.py`.
- **Retry with feedback** — the previously-dead `max_attempts_per_function` knob is wired into the run loop. Default bumped `1 → 3`. On Stage-A/B failure, the last ~1200 chars of the failure log are fed back to the LLM in the next attempt so it can pick better inputs.
- **Purity + self-containment filter** — `_function_extractor.py` gained `_references_only_safe_names`, a scope-aware AST check requiring the body to reference only its own args, Python builtins, and a small stdlib allowlist. Combined with the pre-existing `_BODY_SIDE_EFFECT_PATTERNS` (extended with click/flask context patterns, `sys.stdout/stderr/stdin`, `random.*`, `datetime.now`, `time.sleep`, `warnings.warn`, `tempfile`, `threading`, `asyncio`, `uuid`, `secrets`), this cuts the candidate list to functions that can plausibly be equivalence-tested standalone.
- **Annotation-strip at bake time** — `strip_annotations` in `_eval_script.py` removes all type annotations from the extracted source before writing `task_module.py`. Annotations are evaluated at def-time in Python and their unresolved Names crash import — stripping them is safe (annotations don't affect runtime behaviour) and unlocks functions whose only external refs are in the signature.
- **`is_module_importable` pre-flight** — before spending sandbox time on Stage-A/B, the pipeline compiles the baked stub locally and checks every top-level Name resolves against builtins/known-modules. Catches any remaining bad candidates at zero cost. Emits `stub_module_not_importable` / `oracle_module_not_importable` skip reasons.
- **Recursion-safe rename** — `rename_function_ast` (in `_eval_script.py`) uses `ast.unparse` instead of the v0.7 regex-on-the-def-line, so recursive calls in the body are rewritten too. Previously, `reference_<name>` and `<name>` both silently called the un-renamed name and Stage B trivially passed on incorrect setup.
- **Test-strength gate** — `check_equivalence_test_strength` in `_oss_instruct.py` rejects test files with fewer than 5 `def test_*` functions, tests that reference only one of the two names, or `assert True` / trivial constant asserts.
- **Task dedup** — `_equivalence_fingerprint` combines the function name with a hash of the normalized test body; catches the LLM re-emitting the same test suite on retry.
- **Debug dumps** — every skipped candidate writes its last-attempt test + Stage-A/B log tails to `<out_dir>/.debug_skips/<fn_name>/` so failure-mode audits don't need a re-run.
- **Cross-pipeline coupling cleanup** — `all_tests_passed` moved from `code_instruct.py` to `_eval_script.py`; both pipelines now import from the shared helper.

**Result**: the pipeline emits valid, non-leaky tasks reliably on the candidates it accepts. The tradeoff is that the extractor's purity + importability filter is strict enough that framework-heavy repos (click, flask, starlette) yield only 0–2 pure candidates each. The 100-env reference dataset target has therefore been **deferred to v0.8.8**, contingent on surveying a broader utility-heavy repo set (packaging, itsdangerous, markupsafe, dateutil, etc.).

## Anti-contamination

- Git-history scrub + egress guard from `_env_guard.py`.
- The LLM's test file lands in the emitted task's `tests/`; the `reference_<name>` symbol is *inside the test module* (imported from a separate frozen file baked into the environment), so the agent can't just `import reference_<name>` from `src/` and shortcut.
- **Instruction hygiene**: the instruction says "implement `<name>`" and shows the signature + docstring, but not the original body. The `reference_<name>` name is only visible in the test module (not the instruction).

## LLM use

- **`at bootstrap` (cached)** — reuses `pr_runtime`'s bootstrap.
- **`at synthesis` (per emitted task)** — 1 LLM call per candidate function × `max_attempts_per_function` attempts. The LLM writes only the test, not the solution — smaller output → lower cost than `code_instruct`. ~$0.01–0.05 per emitted task.

## Yield & repo suitability

- **30–60% yield** — fraction of extracted functions where the LLM writes a test that discriminates stub-vs-oracle.
- **Python-only, module-level functions** — class methods (with `self` / `cls`) are deferred to a follow-up. Same limitation as `code_instruct`.
- **Best on repos with lots of small, pure functions** (utility libs, formatting libs, data-processing modules). Struggles on framework-heavy repos where a function's behavior depends on complex fixture state.

## Dependencies

- **`bootstrap/`** — Docker env.
- **`_pr_runtime_verifier.py`** — binary reward today; graded port pending.
- **`_env_guard.py`** — anti-contamination.
- **Function extractor** (`extract_base.py` — patterns lifted from R2E's `repo_builder/fut_extractor/`).
- LiteLLM for the synthesis call.

## Alternatives considered

- **Include class methods via dependency slicing** (bring class context into the isolated test) — attempted; deferred to v0.8 roadmap. Too much complexity for the initial ship.
- **Fuzz + property-based tests** (Hypothesis) instead of LLM-written unit tests — considered; LLM-written tests turn out to catch more real bugs because they exercise the function's *intended* behavior, not just random inputs.

## Rollout plan

Historic. v0.7.0 shipped experimental. Two "v0.8 deferred" items are still open (both listed in `plans/todo.md`):

1. **Iterative test refinement loop** — R2E's `feedback → fix_error → improve_coverage` cycle. Current implementation is single-shot: if the LLM writes a flaky test, we skip rather than retry.
2. **Class-method support** — module-level Python only today.

Graded reward port + reference dataset + promotion to stable also pending, all on v0.9 roadmap.

## Open questions

- **Retry-with-feedback loop shape** — how many retries, what feedback signal (test-output snippet? coverage diff? LLM error-analysis?). Design decision to make in a follow-up mini-RFC when we pick this up.
- **Recursion**: `_rename_function_source` only rewrites the `def` line. If a function calls itself by name, the renamed `reference_<name>` still calls `<name>` internally — usually trips Stage B, the candidate is dropped. Acceptable skip rate in practice, worth logging if the rate ever exceeds ~5%.

## References

- R2E: [ICML '24](https://arxiv.org/abs/2404.11895), [r2e-project/r2e](https://github.com/r2e-project/r2e).

## Implementation

| | |
|---|---|
| **Initial PR** | [#10](https://github.com/huggingface/Repo2RLEnv/pull/10) — v0.7: equivalence_tests + cve_patches |
| **Shipping release** | v0.7.0 (experimental) |
| **Source file** | [`src/repo2rlenv/pipelines/equivalence_tests.py`](../../src/repo2rlenv/pipelines/equivalence_tests.py) |
| **Options model** | [`src/repo2rlenv/spec/options.py`](../../src/repo2rlenv/spec/options.py) — `EquivalenceTestsOptions` |
| **Doc page** | [`docs/pipelines/equivalence_tests.md`](../pipelines/equivalence_tests.md) |
| **Findings / release notes** | *(none yet — publish alongside the graded-reward + iterative-refinement work)* |
| **Reference dataset** | *(none yet — bucket B of `plans/todo.md`)* |
| **Follow-up PRs** | [#55](https://github.com/huggingface/Repo2RLEnv/pull/55) fix grading-test unreachable to non-oracle agents · [#63](https://github.com/huggingface/Repo2RLEnv/pull/63) local + GitLab source · [#69](https://github.com/huggingface/Repo2RLEnv/pull/69) anti-contamination · [#75](https://github.com/huggingface/Repo2RLEnv/pull/75) / [#76](https://github.com/huggingface/Repo2RLEnv/pull/76) Harbor spec sidecar |
