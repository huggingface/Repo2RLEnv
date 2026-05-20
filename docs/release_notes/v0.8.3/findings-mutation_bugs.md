# Arc 5 — `mutation_bugs` sweep findings

**Scope.** Procedural AST-mutation bug injection (SWE-smith style),
Python-only. This arc lands one optimization from plan §4 Arc 5; the
full 20-Python-repo sweep is deferred pending cost approval.

## Optimization landed: pre-container parseability gate

Plan §4 Arc 5 issue (b): "must compile" gate before emission.

Some mutations produce syntactically-broken Python — e.g. an operator
that removes a closing bracket, or one that mis-indents a block. Today
the pipeline still pays a full in-container test run for these. When
the mutated source doesn't parse, every test "fails" by import error —
indistinguishable noise from real broken-test signal.

**Fix.** Add `_is_parseable_python(source)`: wraps `ast.parse` and
returns False on `SyntaxError` / `ValueError`. Wire into the candidate
loop in `MutationBugsPipeline.run()` so unparseable mutations are
dropped with `skip_reason=unparseable_mutation` BEFORE the
`sandbox.exec` call.

Gated by `MutationBugsOptions.skip_unparseable_mutations: bool = True`
(default on). Disable when you specifically want to study the
import-error class of failures.

### Why this matters

A typical mutation-bugs cell runs ~50-100 attempts before emitting
N tasks. Each attempt that goes to the container costs 5-30 s
depending on test-suite size. The parse gate is ~50 µs in Python. At
even a 5% unparseable-mutation rate per cell, the gate saves
~minutes per cell — multiplied by 20 Python repos in the launch, that's
~30-60 minutes of clock saved per sweep.

## Test coverage

6 new unit tests in `tests/test_pipeline_mutation_bugs.py`:

| Test | Covers |
|---|---|
| `test_is_parseable_python_accepts_valid_module` | basic Python parses |
| `test_is_parseable_python_accepts_empty` | empty string is parseable |
| `test_is_parseable_python_rejects_syntax_error` | missing bracket |
| `test_is_parseable_python_rejects_dangling_else` | `else:` at module level |
| `test_is_parseable_python_accepts_subtle_semantic_bug` | `x[-0]` parses (semantic ≠ syntactic) |
| `test_skip_unparseable_mutations_default_enabled` | default-on guard |

Suite at **681 passing**, lint + format clean.

## Acceptance criteria check (this PR)

| Gate | Target | Actual | Pass? |
|---|---|---|---|
| ≥ 1 optimization landed | yes | parseability gate + new option | ✓ |
| Existing tests stay green | 100% | 681/681 + 2 skipped | ✓ |
| Lint + format | clean | clean | ✓ |
| Full 20-Python-repo sweep | yes | **deferred — pending user OK on cost** | — |
| HF dataset published | yes | **deferred — once full sweep lands** | — |

## What's pending for full completion of this arc

1. **Full sweep across 20 Python repos** — should yield ~60 verified
   envs per plan §0. Cost envelope: ~$80-200 (LLM cost for
   `_author_issue_text` per accepted mutation; pipeline itself only
   emits when ≥1 test broke).
2. **HF push** to `AdithyaSK/repo2rlenv-v083-mutation_bugs`.
3. **Findings update** with concrete T3 yield + operator-yield
   distribution (informs plan §4 Arc 5 (a) on which operators to
   weight higher in v0.9).
4. **Verify Sergio's #24 seed-serialization fix didn't regress** —
   plan §4 Arc 5 (d). Today's unit tests don't exercise the
   serialization path; the full sweep does.

## Out of scope for this arc (deferred to v0.9)

- Operator-weight tuning (plan §4 Arc 5 (a)) — needs data from the
  full sweep to inform.
- Instruction-text polish (plan §4 Arc 5 (c)) — the LLM prompt
  `_ISSUE_SYSTEM_PROMPT` is sufficient for the launch.
- Polyglot mutation via tree-sitter — deferred per CLAUDE.md.
