# `equivalence_tests`

R2E-style function-level synthesis. Extract a real Python function from the
target repo as a frozen oracle (`reference_<name>`); ask the LLM to write
equivalence tests that compare a `<name>` candidate against the oracle on
crafted inputs; emit a Harbor task whose gold patch fills in the candidate
with the original implementation.

| | |
|---|---|
| Status | **shipped (v0.7)** — Python module-level functions |
| Sandbox required at gen | Yes |
| LLM required at gen | Yes (writes the test only) |
| Reward kinds emitted | `test_execution` |
| Inspiration | [R2E](https://github.com/r2e-project/r2e) (ICML '24) |

## What's different vs `code_instruct`

| | `code_instruct` | `equivalence_tests` |
|---|---|---|
| Seed | LLM-invented problem | **Real function** from the repo |
| LLM writes | problem + test + solution | **test only** (we already have the solution) |
| Failure surface | LLM might invent unsolvable / wrong problems | LLM might write a bad test (filtered) |
| Yield per repo | one per seed snippet | **one per qualifying function** |

`equivalence_tests` is lower-variance because the ground truth is real
working code, not LLM-imagined behavior.

## Algorithm

```mermaid
flowchart TD
    A[Repo URL] --> B[bootstrap: build env at HEAD]
    B --> C[Walk Python files →<br/>extract module-level fns<br/>matching LOC + filter rules]
    C --> D[LLM: write pytest test<br/>importing <name> and<br/>reference_<name> from task_module]
    D --> E[Syntactic: test uses both names?]
    E --> F[Stage A: <name> stubbed →<br/>test must FAIL]
    F --> G[Stage B: <name> = reference_<name><br/>= original → test must PASS]
    G --> H[Emit Harbor task<br/>(adds task_module.py + test_r2e_<hash>.py)]
```

## Function extractor (R2E-style filters)

Walks `clone_dir.glob("**/*.py")`, applies in order:

1. Exclude path globs (`tests/**`, `docs/**`, `**/__init__.py`, ...)
2. AST parse; skip on `SyntaxError`
3. Module-level `FunctionDef` only (no class methods in v0.7)
4. No `async def`
5. Drop names: dunder, `test_*`, `main`, `setup`, `run`, `init`, `cli`, `wrapper`, `_*`
6. Must have ≥1 positional/keyword arg (no zero-arg)
7. Body LOC ∈ `[min_loc, max_loc]` (default 5–60)
8. Must contain `return <expr>` (not bare `return`)
9. Body must NOT contain side-effect markers (`open(`, `subprocess.`,
   `os.environ`, `requests.`, `print(`, `sys.exit`, `input(`, ...)

Filters are conservative — they keep the candidate pool small but
high-quality. Use `--pipeline-opt min_loc=1 --pipeline-opt max_loc=200`
to loosen for low-LOC repos.

## Reference oracle pattern

The emitted `task_module.py` ships **two** function definitions:

```python
def reference_<name>(...):
    # original implementation, frozen — used as the oracle
    ...

def <name>(...):
    # in the environment image: stubbed (raise NotImplementedError)
    # after the gold patch: identical to reference_<name>
    ...
```

The LLM-generated test imports both and asserts equality across multiple
inputs:

```python
from task_module import <name>, reference_<name>

def test_basic():
    assert <name>(1, 2) == reference_<name>(1, 2)

def test_edge_zero():
    assert <name>(0, 0) == reference_<name>(0, 0)
```

## Verification (two-stage)

| Stage | Module state | Required outcome |
|---|---|---|
| A — stub | `<name>` raises `NotImplementedError`; `reference_<name>` is original | FAIL (else the test is trivial) |
| B — oracle | `<name>` = `reference_<name>` = original | PASS (else the test is buggy) |

Stage A catches the LLM "cheating" with a test that doesn't call `<name>`.
Stage B catches buggy tests (e.g., asserts on outputs that aren't
deterministic across re-runs).

## Options

See `EquivalenceTestsOptions` in `src/repo2rlenv/spec/options.py`.

| Field | Default | Notes |
|---|---|---|
| `limit` | 50 | max emitted tasks |
| `min_loc` / `max_loc` | 5 / 60 | body-LOC range |
| `file_glob` / `exclude_glob` | `**/*.py` / tests/etc. | source selection |
| `seed` | `None` | RNG seed for reproducibility |
| `llm_temperature` | 0.5 | lower than `code_instruct` — tests should be stable |
| `require_test_fails_with_stub` | `True` | Stage A invariant |
| `require_test_passes_with_oracle` | `True` | Stage B invariant |
| `validation_timeout_sec` | 90 | per-candidate test run cap |
| `skip_validation` | `False` | debug; emits without sandbox run |

## End-to-end smoke

```bash
repo2rlenv generate \
  --repo pallets/click \
  --pipeline equivalence_tests \
  --pipeline-opt limit=1 --pipeline-opt seed=42 \
  --llm anthropic/claude-sonnet-4-6 \
  --out ./datasets/click-eqv

harbor run -a oracle -p ./datasets/click-eqv/<task-id>
# Mean reward 1.000
```

## Known v0.7 trade-offs (to revisit)

- **Module-level only.** Class methods (with `self` / `cls`) need either
  dependency slicing (include class context) or method-to-function
  conversion. Deferred to v0.8.
- **Recursion.** `_rename_function_source` rewrites only the `def` line;
  if a function calls itself by name, the renamed `reference_<name>`
  still calls `<name>` internally — usually trips Stage B and the
  candidate is dropped. Acceptable skip rate in practice.
- **No iterative test refinement** (R2E's "feedback → fix_error → improve_coverage"
  loop). Single LLM call per candidate; if the LLM writes a flaky or buggy
  test, we skip rather than retry. The retry loop is on the v0.8 roadmap.

## What we adapted from `references/r2e/`

- Function extractor filter set (`repo_builder/fut_extractor/extract_base.py`):
  LOC bounds, must-have-return, name + decorator + side-effect exclusions.
- The reference-oracle test pattern (`generators/testgen/prompt.py:27-28`):
  `reference_<name>` naming convention; test imports BOTH names.
- The "test must exercise candidate" syntactic guard.

No code is copied. The implementation is original Python stdlib.
