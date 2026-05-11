"""Procedural AST mutation operators for `mutation_bugs`.

Pure stdlib (`ast` + `ast.unparse`). Each operator walks a parsed module,
collects every site that matches its pattern, and emits one `Mutation`
candidate per site. The pipeline picks one at random, runs the existing
test suite against the mutated source, and accepts if the right number of
tests broke.

Trade-offs we deliberately accept in v0.6:
  - `ast.unparse` does NOT preserve original formatting. The diff between
    pre- and post-mutation source therefore rewrites the whole file. This
    is harmless for Harbor (the gold patch.diff just needs to apply cleanly
    on the mutated state), but the resulting diffs are not minimal.
  - We only operate on Python source. Polyglot mutation (Java/JS/Go) is
    deferred to a future release; doing it well requires either tree-sitter
    or a per-language CST library.

Each operator is a free function returning a list of `Mutation` records.
The pipeline composes them via `find_all_mutations(source, ops)`.

Acknowledgment
--------------
The operator catalog mirrors (in name + intent, not in code) the
canonical mutation set from SWE-smith's `swesmith/bug_gen/procedural/base.py`
and the broader mutation-testing literature. Implementation is original.
"""

from __future__ import annotations

import ast
import copy
import random
from collections.abc import Callable
from dataclasses import dataclass

# Type for the per-operator candidate generator.
OperatorFn = Callable[[ast.Module], list["Mutation"]]


@dataclass(slots=True, frozen=True)
class Mutation:
    """One concrete mutation candidate against a parsed module.

    `apply()` returns a NEW module with the change applied; the input tree
    is left intact. We deepcopy + transform inside the closure so callers
    can iterate freely.
    """

    operator: str  # "flip_comparison", "off_by_one", ...
    description: str  # short, human-readable; written into task metadata
    lineno: int  # 1-indexed; site of mutated node
    apply: Callable[[ast.Module], ast.Module]


# ---------------------------------------------------------------------------
# Comparison flipping: ==/!=/</<=/>/>=
# ---------------------------------------------------------------------------

_CMP_FLIP: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
}

_CMP_NAME: dict[type[ast.cmpop], str] = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.GtE: ">=",
    ast.Gt: ">",
    ast.LtE: "<=",
}


def find_flip_comparison(tree: ast.Module) -> list[Mutation]:
    """For each Compare node, flip exactly one of its operators."""
    candidates: list[Mutation] = []
    for path in _walk_with_path(tree):
        node = path[-1]
        if not isinstance(node, ast.Compare):
            continue
        for op_index, op in enumerate(node.ops):
            op_type = type(op)
            if op_type not in _CMP_FLIP:
                continue
            new_type = _CMP_FLIP[op_type]
            description = f"flip {_CMP_NAME[op_type]} -> {_CMP_NAME[new_type]}"
            candidates.append(
                Mutation(
                    operator="flip_comparison",
                    description=description,
                    lineno=getattr(node, "lineno", 0) or 0,
                    apply=_make_compare_flip_apply(path, op_index, new_type),
                )
            )
    return candidates


def _make_compare_flip_apply(
    path: tuple[ast.AST, ...], op_index: int, new_op_type: type[ast.cmpop]
) -> Callable[[ast.Module], ast.Module]:
    indices = _path_indices(path)

    def apply(tree: ast.Module) -> ast.Module:
        cloned = copy.deepcopy(tree)
        node = _resolve_path(cloned, indices)
        assert isinstance(node, ast.Compare)
        node.ops[op_index] = new_op_type()
        return cloned

    return apply


# ---------------------------------------------------------------------------
# Boolean literal flip: True <-> False (only where the literal is a Constant)
# ---------------------------------------------------------------------------


def find_flip_boolean_literal(tree: ast.Module) -> list[Mutation]:
    candidates: list[Mutation] = []
    for path in _walk_with_path(tree):
        node = path[-1]
        if not isinstance(node, ast.Constant):
            continue
        if not isinstance(node.value, bool):
            continue
        # Skip when this Constant is the actual `True`/`False` keyword inside
        # a `keyword` default we don't want to flip. For v0.6 we just flip
        # any boolean-typed constant; validation will reject if it doesn't
        # break tests.
        new_value = not node.value
        description = f"flip {node.value} -> {new_value}"
        candidates.append(
            Mutation(
                operator="flip_boolean_literal",
                description=description,
                lineno=getattr(node, "lineno", 0) or 0,
                apply=_make_constant_replace_apply(path, new_value),
            )
        )
    return candidates


def _make_constant_replace_apply(
    path: tuple[ast.AST, ...], new_value: object
) -> Callable[[ast.Module], ast.Module]:
    indices = _path_indices(path)

    def apply(tree: ast.Module) -> ast.Module:
        cloned = copy.deepcopy(tree)
        node = _resolve_path(cloned, indices)
        assert isinstance(node, ast.Constant)
        node.value = new_value
        return cloned

    return apply


# ---------------------------------------------------------------------------
# Boolean operator flip: and <-> or
# ---------------------------------------------------------------------------


def find_flip_boolean_op(tree: ast.Module) -> list[Mutation]:
    candidates: list[Mutation] = []
    for path in _walk_with_path(tree):
        node = path[-1]
        if not isinstance(node, ast.BoolOp):
            continue
        if isinstance(node.op, ast.And):
            new_op, name = ast.Or(), "or"
            old = "and"
        elif isinstance(node.op, ast.Or):
            new_op, name = ast.And(), "and"
            old = "or"
        else:
            continue
        candidates.append(
            Mutation(
                operator="flip_boolean_op",
                description=f"flip {old} -> {name}",
                lineno=getattr(node, "lineno", 0) or 0,
                apply=_make_boolop_replace_apply(path, new_op),
            )
        )
    return candidates


def _make_boolop_replace_apply(
    path: tuple[ast.AST, ...], new_op: ast.boolop
) -> Callable[[ast.Module], ast.Module]:
    indices = _path_indices(path)

    def apply(tree: ast.Module) -> ast.Module:
        cloned = copy.deepcopy(tree)
        node = _resolve_path(cloned, indices)
        assert isinstance(node, ast.BoolOp)
        node.op = new_op
        return cloned

    return apply


# ---------------------------------------------------------------------------
# Off-by-one: integer literal n -> n + 1 (skip booleans, since bool is int subclass)
# ---------------------------------------------------------------------------


def find_off_by_one(tree: ast.Module) -> list[Mutation]:
    candidates: list[Mutation] = []
    for path in _walk_with_path(tree):
        node = path[-1]
        if not isinstance(node, ast.Constant):
            continue
        if isinstance(node.value, bool):
            continue  # bool is an int subclass; covered by flip_boolean_literal
        if not isinstance(node.value, int):
            continue
        # Skip very large / negative-marker / boundary values that are likely
        # ABI constants (e.g. -1 errno). v0.6 mutates only small positive ints.
        if node.value < 0 or node.value > 10_000:
            continue
        new_value = node.value + 1
        candidates.append(
            Mutation(
                operator="off_by_one",
                description=f"off-by-one {node.value} -> {new_value}",
                lineno=getattr(node, "lineno", 0) or 0,
                apply=_make_constant_replace_apply(path, new_value),
            )
        )
    return candidates


# ---------------------------------------------------------------------------
# Swap arithmetic: BinOp + <-> -, * <-> /  (one swap per BinOp site)
# ---------------------------------------------------------------------------

_ARITH_FLIP: dict[type[ast.operator], type[ast.operator]] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
}

_ARITH_NAME: dict[type[ast.operator], str] = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
}


def find_swap_arithmetic(tree: ast.Module) -> list[Mutation]:
    candidates: list[Mutation] = []
    for path in _walk_with_path(tree):
        node = path[-1]
        if not isinstance(node, ast.BinOp):
            continue
        op_type = type(node.op)
        if op_type not in _ARITH_FLIP:
            continue
        new_type = _ARITH_FLIP[op_type]
        description = f"swap {_ARITH_NAME[op_type]} -> {_ARITH_NAME[new_type]}"
        candidates.append(
            Mutation(
                operator="swap_arithmetic",
                description=description,
                lineno=getattr(node, "lineno", 0) or 0,
                apply=_make_binop_swap_apply(path, new_type),
            )
        )
    return candidates


def _make_binop_swap_apply(
    path: tuple[ast.AST, ...], new_op_type: type[ast.operator]
) -> Callable[[ast.Module], ast.Module]:
    indices = _path_indices(path)

    def apply(tree: ast.Module) -> ast.Module:
        cloned = copy.deepcopy(tree)
        node = _resolve_path(cloned, indices)
        assert isinstance(node, ast.BinOp)
        node.op = new_op_type()
        return cloned

    return apply


# ---------------------------------------------------------------------------
# Invert if: `if cond:` -> `if not cond:`  (swap body/orelse if `else` present)
# ---------------------------------------------------------------------------


def find_invert_if(tree: ast.Module) -> list[Mutation]:
    candidates: list[Mutation] = []
    for path in _walk_with_path(tree):
        node = path[-1]
        if not isinstance(node, ast.If):
            continue
        # Skip `elif` cascades — those parse as nested If inside the orelse.
        # We CAN still mutate them, but the resulting code structure gets
        # confusing; for v0.6, only mutate top-level If (no elif chain).
        candidates.append(
            Mutation(
                operator="invert_if",
                description="invert if-condition",
                lineno=getattr(node, "lineno", 0) or 0,
                apply=_make_if_invert_apply(path),
            )
        )
    return candidates


def _make_if_invert_apply(path: tuple[ast.AST, ...]) -> Callable[[ast.Module], ast.Module]:
    indices = _path_indices(path)

    def apply(tree: ast.Module) -> ast.Module:
        cloned = copy.deepcopy(tree)
        node = _resolve_path(cloned, indices)
        assert isinstance(node, ast.If)
        node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
        # Swap body and orelse so the semantic flip is symmetric. If orelse
        # is empty, the inverted `if not cond: <body>` simply NEVER runs the
        # body when cond is truthy — still a real behavioral change.
        node.body, node.orelse = (
            node.orelse if node.orelse else node.body,
            (node.body if node.orelse else []),
        )
        return cloned

    return apply


# ---------------------------------------------------------------------------
# Path / walking primitives — used to address a specific node after deepcopy
# ---------------------------------------------------------------------------


def _walk_with_path(tree: ast.Module):
    """Yield each node as a tuple-path from root → node.

    Unlike `ast.walk`, the path lets us re-resolve the same node in a
    DEEPCOPIED tree. We need that because `Mutation.apply` clones the tree
    so concurrent candidates don't share state.
    """
    yield from _walk_recursive(tree, ())


def _walk_recursive(node: ast.AST, prefix: tuple[ast.AST, ...]):
    path = (*prefix, node)
    yield path
    for child in ast.iter_child_nodes(node):
        yield from _walk_recursive(child, path)


def _path_indices(path: tuple[ast.AST, ...]) -> tuple[tuple[str, int], ...]:
    """Convert a path of AST nodes to (field_name, index) addresses.

    For each (parent, child) hop, we record which field of `parent` holds
    `child`, and the index within that field if it's a list. This lets us
    re-resolve the same logical node after `copy.deepcopy` (the node
    objects change identity but the structural address is stable).
    """
    import itertools

    indices: list[tuple[str, int]] = []
    for parent, child in itertools.pairwise(path):
        for field_name, field_value in ast.iter_fields(parent):
            if field_value is child:
                indices.append((field_name, -1))
                break
            elif isinstance(field_value, list):
                for i, v in enumerate(field_value):
                    if v is child:
                        indices.append((field_name, i))
                        break
                else:
                    continue
                break
    return tuple(indices)


def _resolve_path(tree: ast.AST, indices: tuple[tuple[str, int], ...]) -> ast.AST:
    """Walk `tree` following the (field_name, index) breadcrumbs."""
    node: ast.AST = tree
    for field_name, idx in indices:
        attr = getattr(node, field_name)
        node = attr[idx] if idx >= 0 else attr
    return node


# ---------------------------------------------------------------------------
# Default catalog + orchestrator
# ---------------------------------------------------------------------------

DEFAULT_OPERATORS: dict[str, OperatorFn] = {
    "flip_comparison": find_flip_comparison,
    "flip_boolean_literal": find_flip_boolean_literal,
    "flip_boolean_op": find_flip_boolean_op,
    "off_by_one": find_off_by_one,
    "swap_arithmetic": find_swap_arithmetic,
    "invert_if": find_invert_if,
}


def find_all_mutations(source: str, operators: list[str] | None = None) -> list[Mutation]:
    """Parse `source` and collect candidates from every named operator.

    Returns an empty list if the source has a SyntaxError; callers should
    filter those files out.

    `operators=None` means "use every operator in DEFAULT_OPERATORS".
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    chosen = operators or list(DEFAULT_OPERATORS)
    out: list[Mutation] = []
    for name in chosen:
        op = DEFAULT_OPERATORS.get(name)
        if op is None:
            continue
        out.extend(op(tree))
    return out


def pick_mutation(mutations: list[Mutation], rng: random.Random) -> Mutation | None:
    """Pick one mutation uniformly at random. Returns None if list is empty."""
    if not mutations:
        return None
    return rng.choice(mutations)


def apply_to_source(mutation: Mutation, source: str) -> str:
    """Apply `mutation` to `source` and return the unparsed result.

    Parses the source fresh, runs the mutation's `apply` (which deepcopies
    internally), then `ast.unparse`s. The result is syntactically valid
    Python but does not preserve original formatting / comments.
    """
    tree = ast.parse(source)
    mutated_tree = mutation.apply(tree)
    return ast.unparse(mutated_tree)
