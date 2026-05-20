# Arc 8 — `refactor_synthesis` sweep findings

**Scope.** Python-native rename-refactor mining. This arc lands one
optimization from plan §4 Arc 8; the full 20-Python-repo sweep is
deferred pending cost approval.

## Optimization landed: minimum-callsite scope filter

Plan §4 Arc 8 issue (a): trivial one-symbol renames make weak tasks.
The agent applies a single-line change and is done — no real reasoning
exercised.

**Fix.** Add `count_callsite_changes(diff, *, old_name, new_name)` to
`_rename_detector`: walks the unified diff and counts how many `-` /
`+` lines mention the old / new name as a **word** (regex word boundary,
so `foo` doesn't match `foobar`). The def / class signature line is
excluded from the count — the verifier already requires both ends of
the rename to be present.

New option `RefactorSynthesisOptions.min_callsites: int = 1` (default).
A rename whose `added` count falls below this threshold gets skipped
with `reason=too_few_callsites`. Set 0 to disable the filter; set
higher per-arc to demand larger-scope refactors (e.g. min_callsites=5
for "must touch ≥ 5 usages").

## Test coverage

5 new unit tests in `tests/test_rename_detector.py`:

| Test | Covers |
|---|---|
| `test_callsite_count_trivial_rename_one_def_no_callsites` | def-only rename → 0/0 |
| `test_callsite_count_renames_with_two_callsites` | def + 2 changes → (2, 2) |
| `test_callsite_count_word_boundary` | `foo` ≠ `foobar` |
| `test_callsite_count_class_rename` | class signature excluded from count |
| `test_callsite_count_empty_diff` | empty diff → (0, 0) |

Suite at **703 passing**, lint + format clean.

## Acceptance criteria check (this PR)

| Gate | Target | Actual | Pass? |
|---|---|---|---|
| ≥ 1 optimization landed | yes | min-callsite scope filter | ✓ |
| Existing tests stay green | 100% | 703/703 + 2 skipped | ✓ |
| Lint + format | clean | clean | ✓ |
| Full 20-Python-repo sweep | yes | **deferred — pending user OK on cost** | — |
| HF dataset published | yes | **deferred — once full sweep lands** | — |

## What's pending for full completion of this arc

1. **Full sweep across 20 Python repos** — should yield ~55 verified
   envs per plan §0. Cost envelope ~$50-100 (refactor_synthesis is
   diff-driven, no per-task LLM; bootstrap LLM only for fresh repos).
2. **HF push** to `AdithyaSK/repo2rlenv-v083-refactor_synthesis`.
3. **Findings update** with concrete T3 yield + a callsite-count
   distribution (informs whether `min_callsites=1` or a higher
   default makes sense for the launch).

## Out of scope for this arc (deferred to v0.9)

- Instruction wording — describe the WHY (plan §4 Arc 8 (b)).
- Extract Method / Inline kinds (plan §4 Arc 8 (c), already noted as
  deferred in plans/CLAUDE.md).
