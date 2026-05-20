# Arc 6 — `code_instruct` sweep findings

**Scope.** OSS-Instruct-style synthesized coding tasks (Python-only).
This arc lands one optimization from plan §4 Arc 6; the full
20-Python-repo sweep is deferred pending cost approval.

## Optimization landed: no-op stub tautology check

Plan §4 Arc 6 issue (c): reject verifiers that pass with a no-op patch
(i.e., the test isn't actually exercising oracle behavior — it only
asserts importability).

The pipeline already does a 2-stage verifier check:

- **Stage A**: write only the test, run pytest → must FAIL.
- **Stage B**: write test + oracle, run pytest → must PASS.

The gap: Stage A only fails on `ImportError`. A test that imports the
oracle but then only checks `from task_module import foo; assert foo`
(truthiness of the function object, not its behavior) would FAIL stage A
(import error) but PASS stage B (oracle imports fine) — and STILL be
useless as a verifier because any function-shaped no-op would also pass.

**Fix.** Insert Stage A.5 between A and B:

1. Build a stub `task_module.py` from the LLM's solution code. Every
   top-level function becomes `def <name>(*args, **kwargs): return None`;
   every top-level class becomes `class <Name>: pass`; imports and
   constants are dropped.
2. Run pytest against the test file + this stub. If the tests **pass**,
   reject the candidate with `reason=test_passes_with_noop_stub`.

The verifier now requires that the test exercises the oracle's
*behavior*, not just its existence.

Gated by `CodeInstructOptions.require_test_fails_with_noop_stub`
(default `True`). Disable per-arc for higher emission yield at the
cost of some verifier-quality slack.

## Test coverage

7 new unit tests in `tests/test_pipeline_code_instruct.py`:

| Test | Covers |
|---|---|
| `test_noop_stub_function` | single function → no-op stub |
| `test_noop_stub_multiple_symbols` | mixed funcs + classes |
| `test_noop_stub_async_function` | async def preserved |
| `test_noop_stub_drops_imports_and_constants` | only callables / classes in stub |
| `test_noop_stub_unparseable_returns_empty` | caller can skip stage on bad LLM output |
| `test_noop_stub_no_callables_returns_empty` | imports/constants-only solution → no stub work |
| `test_noop_stub_module_is_syntactically_valid` | stub itself round-trips through `ast.parse` |

Plus a flag-default assertion. Suite at **688 passing**, lint + format clean.

## Acceptance criteria check (this PR)

| Gate | Target | Actual | Pass? |
|---|---|---|---|
| ≥ 1 optimization landed | yes | no-op stub tautology check | ✓ |
| Existing tests stay green | 100% | 688/688 + 2 skipped | ✓ |
| Lint + format | clean | clean | ✓ |
| Full 20-Python-repo sweep | yes | **deferred — pending user OK on cost** | — |
| HF dataset published | yes | **deferred — once full sweep lands** | — |

## What's pending for full completion of this arc

1. **Full sweep across 20 Python repos** — should yield ~60 verified
   envs per plan §0. Cost envelope ~$80-150 (LLM-heavy: each task
   requires a Sonnet call for problem + test + solution).
2. **HF push** to `AdithyaSK/repo2rlenv-v083-code_instruct`.
3. **Findings update** with concrete T3 yield + impact of the new
   stub stage (count `test_passes_with_noop_stub` skip reasons across
   cells).

## Out of scope for this arc (deferred to v0.9)

- Haiku-judged verifier-quality second pass (plan §4 Arc 6 (a)) —
  the stub check covers most tautological patterns; LLM-judged adds
  marginal value at high cost.
- Curated instruction-difficulty distribution (plan §4 Arc 6 (b)) —
  requires sweep data to calibrate.
