"""Select repository test evidence statically, without importing target code."""

from __future__ import annotations

import ast
from pathlib import Path


def _methods(node: ast.AST, parents: tuple[str, ...] = ()):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            yield from _methods(child, (*parents, child.name))
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield parents, child


def test_excerpts(root: Path, identities: list[str]) -> dict[str, str]:
    excerpts = {}
    for path in sorted(root.rglob("test*.py")):
        module = path.relative_to(root).with_suffix("").as_posix().replace("/", ".")
        relevant = [
            identity
            for identity in identities
            if identity.startswith(module + ".") or identity.startswith(module + "::")
        ]
        if not relevant:
            continue
        source = path.read_text()
        tree = ast.parse(source)
        nodes = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]

        for parents, node in _methods(tree):
            prefix = ".".join((module, *parents)) + "::" + node.name
            if any(identity.split("[", 1)[0] == prefix for identity in relevant):
                nodes.append(node)
        excerpts[path.relative_to(root).as_posix()] = "\n\n".join(
            ast.get_source_segment(source, node) or "" for node in nodes
        )
    if not excerpts:
        raise ValueError("Could not resolve failing test evidence")
    return excerpts
