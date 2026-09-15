"""Group observed core dependencies and introduce only newly developed functions."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path


def function_catalog(base: Path, paths: list[str]) -> dict:
    """This first profile covers top-level synchronous Python functions."""
    found = {}
    for relative in paths:
        source = base / relative
        for path in [source] if source.is_file() else sorted(source.rglob("*.py")):
            text = path.read_text()
            tree = ast.parse(text)
            name = path.relative_to(base).as_posix()
            for node in tree.body:
                if not isinstance(node, ast.FunctionDef):
                    continue
                start = min([node.lineno] + [item.lineno for item in node.decorator_list])
                key = f"{name}:{start}:{node.name}"
                found[key] = {
                    "path": name,
                    "name": node.name,
                    "line": node.lineno,
                    "source": ast.get_source_segment(text, node),
                }
    return found


def development_schedule(traces: list[dict], catalog: dict) -> list[dict]:
    groups = defaultdict(list)
    for trace in traces:
        if trace.get("truncated"):
            continue
        core = frozenset(set(trace["core_nodes"]) & catalog.keys())
        if core:
            groups[core].append(trace)
    developed, previous_tests, result = set(), [], []
    for core, records in sorted(groups.items(), key=lambda item: (len(item[0]), sorted(item[0]))):
        new = core - developed
        tests = [record["test_id"] for record in records]
        targets = set().union(*(set(record["target_nodes"]) for record in records)) & new
        if new and targets:
            result.append(
                {
                    "step": len(result),
                    "core_nodes": sorted(core),
                    "nodes_to_develop": sorted(new),
                    "target_nodes": sorted(targets),
                    "dependent_nodes": sorted(new - targets),
                    "test_ids": tests,
                    "previous_tests": list(previous_tests),
                    "edges": sorted(set(tuple(edge) for row in records for edge in row["edges"])),
                }
            )
        developed.update(new)
        previous_tests.extend(tests)
    return result


def skeletonize(source: str, *, path: str, schedule: dict, docstrings: dict | None = None) -> str:
    """Stub target entry points and remove newly introduced dependencies."""
    tree = ast.parse(source)
    docstrings = docstrings or {}
    kept = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            start = min([node.lineno] + [item.lineno for item in node.decorator_list])
            key = f"{path}:{start}:{node.name}"
            if key in schedule["dependent_nodes"]:
                continue
            if key in schedule["target_nodes"]:
                node.body = [
                    ast.Expr(
                        ast.Constant(docstrings.get(key, "Implement the documented behavior."))
                    ),
                    ast.Expr(ast.Constant(Ellipsis)),
                ]
        kept.append(node)
    tree.body = kept
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n"
