"""Formatting-preserving procedural mutation strategies inspired by SWE-smith.

Source: SWE-bench/SWE-smith, MIT, 9b74ac08118a85c39c356802f7961893af73e07f,
bug_gen/procedural/python/{operations,control_flow}.py. Owned single-site
enumeration replaces upstream probabilistic multi-edit sampling. LibCST is an
ordinary parsing library; no upstream research package is imported.
"""

from __future__ import annotations

import difflib
import hashlib
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Mutation:
    id: str
    operator: str
    entity: str
    line: int
    source: str
    patch: str


def generate_mutations(
    source: str, filename: str, *, seed: int = 0, limit: int = 100
) -> list[Mutation]:
    """Enumerate source-valid edits inside functions, retaining surrounding text."""
    import libcst as cst
    from libcst.metadata import MetadataWrapper, PositionProvider

    from repo2rlenv.emitter.bundle import relative_asset_path

    relative_asset_path("environment/" + filename)
    if limit < 1:
        raise ValueError("Mutation limit must be positive")
    wrapper = MetadataWrapper(cst.parse_module(source))
    positions = wrapper.resolve(PositionProvider)
    sites: list[tuple[cst.CSTNode, cst.CSTNode, str, str]] = []
    flips = {
        cst.Add: cst.Subtract,
        cst.Subtract: cst.Add,
        cst.Multiply: cst.Divide,
        cst.Divide: cst.Multiply,
        cst.FloorDivide: cst.Modulo,
        cst.Modulo: cst.FloorDivide,
        cst.And: cst.Or,
        cst.Or: cst.And,
        cst.Equal: cst.NotEqual,
        cst.NotEqual: cst.Equal,
        cst.LessThan: cst.GreaterThan,
        cst.GreaterThan: cst.LessThan,
        cst.LessThanEqual: cst.GreaterThanEqual,
        cst.GreaterThanEqual: cst.LessThanEqual,
        cst.In: cst.NotIn,
        cst.NotIn: cst.In,
        cst.Is: cst.IsNot,
        cst.IsNot: cst.Is,
    }

    class Collect(cst.CSTVisitor):
        def __init__(self):
            self.scope: list[str] = []
            self.function_depth = 0

        def visit_ClassDef(self, node):
            self.scope.append(node.name.value)

        def leave_ClassDef(self, node):
            self.scope.pop()

        def visit_FunctionDef(self, node):
            self.scope.append(node.name.value)
            self.function_depth += 1

        def leave_FunctionDef(self, node):
            self.scope.pop()
            self.function_depth -= 1

        def add(self, original, replacement, operator):
            if self.function_depth:
                sites.append((original, replacement, operator, ".".join(self.scope)))

        def visit_BinaryOperation(self, node):
            replacement = flips.get(type(node.operator))
            if replacement:
                self.add(node, node.with_changes(operator=replacement()), "flip_operator")

        visit_BooleanOperation = visit_BinaryOperation
        visit_ComparisonTarget = visit_BinaryOperation

        def visit_If(self, node):
            self.add(
                node,
                node.with_changes(
                    test=cst.UnaryOperation(
                        cst.Not(),
                        cst.ensure_type(node.test, cst.BaseExpression).with_changes(
                            lpar=[cst.LeftParen()], rpar=[cst.RightParen()]
                        ),
                    )
                ),
                "invert_condition",
            )

        def visit_Integer(self, node):
            value = node.evaluated_value
            if abs(value) <= 1_000_000:
                for delta in (-1, 1):
                    self.add(node, cst.parse_expression(str(value + delta)), "change_constant")

    wrapper.module.visit(Collect())
    random.Random(seed).shuffle(sites)
    result: list[Mutation] = []
    seen: set[str] = set()

    class Replace(cst.CSTTransformer):
        def __init__(self, original, replacement):
            self.original, self.replacement = original, replacement

        def on_leave(self, original_node, updated_node):
            return self.replacement if original_node is self.original else updated_node

    for original, replacement, operator, entity in sites:
        mutated = wrapper.module.visit(Replace(original, replacement)).code
        if mutated == source:
            continue
        # Parse only: generated task code is never executed on the controller.
        try:
            cst.parse_module(mutated)
        except cst.ParserSyntaxError:
            continue
        identity = hashlib.sha256((filename + "\0" + mutated).encode()).hexdigest()
        if identity in seen:
            continue
        seen.add(identity)
        patch = "".join(
            difflib.unified_diff(
                source.splitlines(keepends=True),
                mutated.splitlines(keepends=True),
                fromfile=f"a/{filename}",
                tofile=f"b/{filename}",
            )
        )
        result.append(
            Mutation(
                identity[:20], operator, entity, positions[original].start.line, mutated, patch
            )
        )
        if len(result) >= limit:
            break
    return result
