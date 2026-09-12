"""Python entity and test correspondence filters used by historical recipes."""

from __future__ import annotations

import ast
from pathlib import PurePosixPath


def within(path: str, roots: list[str]) -> bool:
    value = PurePosixPath(path)
    return any(
        value == PurePosixPath(root) or PurePosixPath(root) in value.parents for root in roots
    )


def entities(source: str) -> tuple[dict[str, str], list[str]]:
    tree = ast.parse(source)
    definitions = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body = (
                node.body[1:] if isinstance(node, ast.Module) else node.body[1:] or [ast.Pass()]
            )

    class Visitor(ast.NodeVisitor):
        prefix = ""

        def definition(self, node):
            previous = self.prefix
            self.prefix += node.name + "."
            definitions[self.prefix[:-1]] = ast.dump(node, include_attributes=False)
            self.generic_visit(node)
            self.prefix = previous

        visit_FunctionDef = definition
        visit_AsyncFunctionDef = definition
        visit_ClassDef = definition

    Visitor().visit(tree)
    statements = [
        ast.dump(node, include_attributes=False)
        for node in tree.body
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]
    return definitions, statements


def check_entities(
    source_pairs: dict[str, tuple[str, str]], test_pairs: dict[str, tuple[str, str]], options
) -> dict:
    added, edited, deleted, statements = [], [], [], 0
    test_changes = []
    for path, (old, new) in source_pairs.items():
        before, before_statements = entities(old)
        after, after_statements = entities(new)
        added.extend(f"{path}:{name}" for name in after.keys() - before.keys())
        deleted.extend(f"{path}:{name}" for name in before.keys() - after.keys())
        edited.extend(
            f"{path}:{name}" for name in before.keys() & after.keys() if before[name] != after[name]
        )
        statements += len(set(before_statements) ^ set(after_statements))
    for path, (old, new) in test_pairs.items():
        before, _ = entities(old)
        after, _ = entities(new)
        for name in after:
            if before.get(name) != after[name]:
                test_changes.append({"file": path, "name": name, "code": after[name]})
    if not (added or edited or statements):
        raise ValueError("No non-docstring Python behavior change")
    if not test_changes:
        raise ValueError("No added or modified test entity")
    if options.require_bug_edit and (
        deleted
        or not edited
        or len(added) > options.max_added_entities
        or len(edited) > options.max_edited_entities
        or statements > options.max_statement_entities
    ):
        raise ValueError("Outside the native bug-edit entity bounds")
    matched = any("issue" in item["name"].lower() for item in test_changes)
    for entity in edited:
        name = entity.split(":", 1)[1].split(".")[-1]
        matched |= any(
            name in item["name"]
            or f"id='{name}'" in item["code"]
            or f"attr='{name}'" in item["code"]
            for item in test_changes
        )
    if options.require_test_match and not matched:
        raise ValueError("Edited test does not name or call a changed source entity")
    return {
        "added": sorted(added),
        "edited": sorted(edited),
        "deleted": sorted(deleted),
        "statement_changes": statements,
        "test_match": matched,
        "test_entities": [{"file": row["file"], "name": row["name"]} for row in test_changes],
    }
