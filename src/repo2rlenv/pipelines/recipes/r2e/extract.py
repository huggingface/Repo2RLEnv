"""Bounded module-level dependency slices without executing repository imports."""

from __future__ import annotations

import ast


def dependency_slice(source: str, function_name: str) -> str:
    tree = ast.parse(source)
    bindings = {}
    for index, node in enumerate(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.asname or alias.name.split(".")[0] for alias in node.names]
        else:
            names = [
                item.id
                for item in ast.walk(node)
                if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
            ]
        for name in names:
            bindings[name] = index
    pending, selected = [function_name], set()
    while pending:
        name = pending.pop()
        index = bindings.get(name)
        if index is None or index in selected:
            continue
        selected.add(index)
        pending.extend(
            item.id
            for item in ast.walk(tree.body[index])
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
        )
    result = ast.unparse(
        ast.Module(body=[tree.body[index] for index in sorted(selected)], type_ignores=[])
    )
    if len(result) > 24000:
        raise ValueError("Dependency slice exceeds the initial R2E context profile")
    return result


def stub(
    source: str, function_name: str, docstring: str = "Implement the specified behavior."
) -> str:
    tree = ast.parse(source)
    found = False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            node.body = [
                ast.Expr(ast.Constant(docstring)),
                ast.Raise(ast.Call(ast.Name("NotImplementedError", ast.Load()), [], [])),
            ]
            found = True
    if not found:
        raise ValueError("Target function is absent from the module")
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n"
