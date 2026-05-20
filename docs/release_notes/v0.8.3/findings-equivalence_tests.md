# Arc 7 — `equivalence_tests` sweep findings

**Scope.** R2E-style function-level equivalence-test synthesis (Python-only).
This arc lands one optimization from plan §4 Arc 7; the full
20-Python-repo sweep is deferred pending cost approval.

## Optimization landed: AST-precise side-effect detection

Plan §4 Arc 7 issue (b): equivalence tests are brittle for functions
that touch global state, I/O, or have side effects — the test would
need to mock / fixture-stub each side effect to compare candidate vs.
oracle outputs.

The v0.8.1 implementation used a substring match on the function source
(`"open("`, `"print("`, etc.). Two issues with that approach:

1. **False positives**: `"open("` matches `"reopen("`, `"print("`
   matches `"sprint("`, etc.
2. **Missed patterns**: `global` / `nonlocal` statements (read/write
   module state), generators (`yield` / `yield from`), and various
   dotted-attribute calls that the prefix list didn't enumerate.

**Fix.** Add `_ast_has_side_effect(node)` — a precise AST walker that:

- Returns `(is_side_effect, reason_kind)` where reason is one of
  `"global"`, `"nonlocal"`, `"yield"`, or `"forbidden_call"`.
- Walks the function body via `ast.iter_child_nodes`, **stopping at
  nested FunctionDef / ClassDef boundaries** — a nested helper having
  its own side effects shouldn't disqualify the outer function.
- Uses `_is_forbidden_call(node)` to match calls against a name list
  (`open / print / input / exec / eval / exit / quit`) AND a dotted
  attribute prefix list (`os. / sys. / shutil. / subprocess. /
  logging. / requests. / http. / urllib. / socket.`).

Both filters are wired into `extract_from_module` so candidates are
rejected before the (expensive) LLM stage. The existing string-based
heuristic is kept as a cheap pre-pass since it catches a few patterns
the AST walker doesn't.

## Test coverage

10 new unit tests in `tests/test_function_extractor.py`:

| Test | Covers |
|---|---|
| `test_ast_se_global_statement` | `global X` → reason="global" |
| `test_ast_se_yield_is_side_effect` | generator → reason="yield" |
| `test_ast_se_yield_from` | `yield from` → reason="yield" |
| `test_ast_se_open_call` | bare `open()` → reason="forbidden_call" |
| `test_ast_se_dotted_os_call` | `os.path.exists(...)` → forbidden_call |
| `test_ast_se_pure_function_passes` | clean function → not flagged |
| `test_ast_se_does_not_descend_into_nested_fn` | inner-fn side effect doesn't taint outer |
| `test_is_forbidden_call_reopen_no_false_positive` | `reopen(...)` → False |
| `test_pipeline_filters_global_statement` | end-to-end: global → extractor drops it |
| `test_pipeline_filters_generator` | end-to-end: yield → extractor drops it |

Suite at **698 passing**, lint + format clean.

## Acceptance criteria check (this PR)

| Gate | Target | Actual | Pass? |
|---|---|---|---|
| ≥ 1 optimization landed | yes | AST-precise side-effect detection | ✓ |
| Existing tests stay green | 100% | 698/698 + 2 skipped | ✓ |
| Lint + format | clean | clean | ✓ |
| Full 20-Python-repo sweep | yes | **deferred — pending user OK on cost** | — |
| HF dataset published | yes | **deferred — once full sweep lands** | — |

## What's pending for full completion of this arc

1. **Full sweep across 20 Python repos** — should yield ~60 verified
   envs per plan §0. Cost envelope ~$80-150 (LLM stages: function
   extraction is free; equivalence-test synthesis per accepted seed
   uses Sonnet).
2. **HF push** to `AdithyaSK/repo2rlenv-v083-equivalence_tests`.
3. **Findings update** with concrete T3 yield + a side-effect-kind
   distribution (informs which patterns are most common across the
   launch repos).

## Out of scope for this arc (deferred to v0.9)

- Function complexity scoring beyond LOC (plan §4 Arc 7 (a)) — could
  use cyclomatic complexity once we have sweep data to calibrate.
- Async functions are already skipped via the
  `ast.AsyncFunctionDef` early-return in `extract_from_module`.
- Iterative refinement loop for failed equivalence tests (plan §11).
