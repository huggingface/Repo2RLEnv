"""Small single-point bugs injected into Python sources, used to check that tests catch real bugs.

Each mutant changes one thing: a comparison (< becomes <=), an arithmetic operator (+ becomes -),
a boolean operator (and becomes or), an integer constant (n becomes n + 1), or a return value
(becomes None).
"""

from __future__ import annotations

import ast
import copy
import random
import re

SWAPPED_COMPARISONS = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}
SWAPPED_OPERATORS = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.FloorDiv,
    ast.FloorDiv: ast.Mult,
}
TEST_FILE = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*\.py$")


def mutate(node: ast.AST) -> bool:
    """Mutate `node` in place if it is a mutation site; return whether it was."""
    if isinstance(node, ast.Compare) and type(node.ops[0]) in SWAPPED_COMPARISONS:
        node.ops[0] = SWAPPED_COMPARISONS[type(node.ops[0])]()
    elif isinstance(node, ast.BinOp) and type(node.op) in SWAPPED_OPERATORS:
        node.op = SWAPPED_OPERATORS[type(node.op)]()
    elif isinstance(node, ast.BoolOp):
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
    elif isinstance(node, ast.Constant) and type(node.value) is int:
        node.value += 1
    elif isinstance(node, ast.Return) and node.value is not None:
        node.value = ast.Constant(None)
    else:
        return False
    return True


def make_mutants(files: dict[str, str], count: int = 16, seed: int = 0) -> list[dict[str, str]]:
    """Up to `count` copies of `files`, each with one mutation in a non-test Python source."""
    sites = []
    for path, source in files.items():
        if not path.endswith(".py") or TEST_FILE.search(path):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        sites += [
            (path, tree, index)
            for index, node in enumerate(ast.walk(tree))
            if mutate(copy.deepcopy(node))
        ]
    random.Random(seed).shuffle(sites)
    mutants = []
    for path, tree, site_index in sites[:count]:
        mutated_tree = copy.deepcopy(tree)
        mutate(
            next(node for index, node in enumerate(ast.walk(mutated_tree)) if index == site_index)
        )
        mutants.append({**files, path: ast.unparse(ast.fix_missing_locations(mutated_tree))})
    return mutants
