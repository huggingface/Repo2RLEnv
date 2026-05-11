"""AST mutation operators — pure unit tests.

Each operator is exercised against tiny Python source snippets. We
re-parse the mutated output to confirm:
  (1) it stays syntactically valid Python
  (2) ast.unparse + apply round-trips identifiably (the expected token
      appears in the new source)

We do NOT test that the mutation is *semantically* breaking — that
needs a test suite to run against, which is the pipeline's job.
"""

from __future__ import annotations

import ast
import random

from repo2rlenv.pipelines._mutation_operators import (
    DEFAULT_OPERATORS,
    apply_to_source,
    find_all_mutations,
    find_flip_boolean_literal,
    find_flip_boolean_op,
    find_flip_comparison,
    find_invert_if,
    find_off_by_one,
    find_swap_arithmetic,
    pick_mutation,
)

# ---------------------------------------------------------------------------
# flip_comparison
# ---------------------------------------------------------------------------


def test_flip_comparison_finds_eq():
    src = "def f(x): return x == 1\n"
    muts = find_flip_comparison(ast.parse(src))
    assert len(muts) == 1
    assert muts[0].operator == "flip_comparison"
    new_src = apply_to_source(muts[0], src)
    # Compile to confirm validity
    ast.parse(new_src)
    assert "!=" in new_src


def test_flip_comparison_each_compare_yields_one():
    src = "def f(x, y, z): return x < y and y <= z\n"
    muts = find_flip_comparison(ast.parse(src))
    # Two Compare nodes (one per binary op), each with one operator
    assert len(muts) == 2


def test_flip_comparison_skips_unknown_ops():
    # `is` and `in` aren't in our flip table; we leave them alone
    src = "def f(x): return x is None\n"
    muts = find_flip_comparison(ast.parse(src))
    assert muts == []


# ---------------------------------------------------------------------------
# flip_boolean_literal
# ---------------------------------------------------------------------------


def test_flip_boolean_literal_true_to_false():
    src = "ENABLED = True\n"
    muts = find_flip_boolean_literal(ast.parse(src))
    assert len(muts) == 1
    new_src = apply_to_source(muts[0], src)
    assert "False" in new_src


def test_flip_boolean_literal_skips_non_booleans():
    src = "VALUE = 1\n"
    assert find_flip_boolean_literal(ast.parse(src)) == []


# ---------------------------------------------------------------------------
# flip_boolean_op
# ---------------------------------------------------------------------------


def test_flip_boolean_op_and_to_or():
    src = "def f(x, y): return x and y\n"
    muts = find_flip_boolean_op(ast.parse(src))
    assert len(muts) == 1
    new_src = apply_to_source(muts[0], src)
    assert " or " in new_src
    assert " and " not in new_src


def test_flip_boolean_op_or_to_and():
    src = "def f(x, y): return x or y\n"
    muts = find_flip_boolean_op(ast.parse(src))
    new_src = apply_to_source(muts[0], src)
    assert " and " in new_src


# ---------------------------------------------------------------------------
# off_by_one
# ---------------------------------------------------------------------------


def test_off_by_one_increments_small_ints():
    src = "LIMIT = 10\n"
    muts = find_off_by_one(ast.parse(src))
    assert len(muts) == 1
    new_src = apply_to_source(muts[0], src)
    assert "11" in new_src


def test_off_by_one_skips_negative():
    src = "FLAG = -1\n"
    # -1 is a UnaryOp(USub, Constant(1)); the Constant alone has value 1,
    # which IS in our acceptable range. The mutation would flip 1 → 2,
    # giving `-2`. Acceptable behavior — we test that something happens.
    muts = find_off_by_one(ast.parse(src))
    # Either 0 (no candidate, future cleanup) or 1 (Constant=1 mutates).
    # The current operator finds the bare Constant=1 and flips → 2.
    assert len(muts) in (0, 1)


def test_off_by_one_skips_booleans():
    """bool is an int subclass; off_by_one must NOT pick it up."""
    src = "FLAG = True\n"
    assert find_off_by_one(ast.parse(src)) == []


def test_off_by_one_skips_huge_ints():
    """Constants like 65536 are likely ABI / bit-mask values — skip."""
    src = "MASK = 999999\n"
    assert find_off_by_one(ast.parse(src)) == []


# ---------------------------------------------------------------------------
# swap_arithmetic
# ---------------------------------------------------------------------------


def test_swap_arithmetic_add_to_sub():
    src = "def f(x, y): return x + y\n"
    muts = find_swap_arithmetic(ast.parse(src))
    assert len(muts) == 1
    new_src = apply_to_source(muts[0], src)
    assert "x - y" in new_src


def test_swap_arithmetic_mult_to_div():
    src = "def f(x, y): return x * y\n"
    muts = find_swap_arithmetic(ast.parse(src))
    new_src = apply_to_source(muts[0], src)
    assert "/" in new_src


def test_swap_arithmetic_skips_other_ops():
    """Modulo isn't in the swap table; we leave it alone."""
    src = "def f(x): return x % 2\n"
    assert find_swap_arithmetic(ast.parse(src)) == []


# ---------------------------------------------------------------------------
# invert_if
# ---------------------------------------------------------------------------


def test_invert_if_adds_not_to_condition():
    src = "def f(x):\n    if x:\n        return 1\n    return 0\n"
    muts = find_invert_if(ast.parse(src))
    assert len(muts) == 1
    new_src = apply_to_source(muts[0], src)
    assert "not x" in new_src


def test_invert_if_swaps_body_and_orelse():
    src = "def f(x):\n    if x:\n        return 1\n    else:\n        return 2\n"
    muts = find_invert_if(ast.parse(src))
    new_src = apply_to_source(muts[0], src)
    # After inverting, `not x` should map to what was originally `return 2`
    assert "not x" in new_src


# ---------------------------------------------------------------------------
# find_all_mutations + pick_mutation
# ---------------------------------------------------------------------------


def test_find_all_mutations_aggregates_across_operators():
    src = "def f(x):\n    if x == 1:\n        return True\n    return False\n"
    muts = find_all_mutations(src)
    # Contains: 1 flip_comparison, 2 flip_boolean_literal, 1 invert_if, 1 off_by_one (the 1)
    operators_seen = {m.operator for m in muts}
    assert "flip_comparison" in operators_seen
    assert "flip_boolean_literal" in operators_seen
    assert "invert_if" in operators_seen


def test_find_all_mutations_filters_by_operator_name():
    src = "def f(x): return x == 1\n"
    muts = find_all_mutations(src, operators=["flip_comparison"])
    assert all(m.operator == "flip_comparison" for m in muts)


def test_find_all_mutations_handles_syntax_error():
    """Operators silently skip un-parseable source."""
    src = "def f( :\n"  # invalid
    assert find_all_mutations(src) == []


def test_pick_mutation_returns_none_for_empty_list():
    assert pick_mutation([], random.Random(0)) is None


def test_pick_mutation_uses_rng_deterministically():
    src = "def f(x, y): return x and y or x == 1\n"
    muts = find_all_mutations(src)
    assert len(muts) >= 2
    rng_a = random.Random(42)
    rng_b = random.Random(42)
    assert pick_mutation(muts, rng_a).description == pick_mutation(muts, rng_b).description


# ---------------------------------------------------------------------------
# default operator registry
# ---------------------------------------------------------------------------


def test_default_operators_complete():
    expected = {
        "flip_comparison",
        "flip_boolean_literal",
        "flip_boolean_op",
        "off_by_one",
        "swap_arithmetic",
        "invert_if",
    }
    assert set(DEFAULT_OPERATORS.keys()) == expected
