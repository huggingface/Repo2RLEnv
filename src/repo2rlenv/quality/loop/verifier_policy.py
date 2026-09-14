"""Reject common source-text grading mistakes in Tasksmith's behavioral tests.

This narrow static check is not a completeness or reward-hacking proof. Runtime
controls and semantic review still establish whether the verifier tests behavior.
"""

from __future__ import annotations

import ast

TASKSMITH_BEHAVIOR_PATH = "tests/source/tests/tasksmith_behavior.py"


def _is_module_path(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "__file__":
        return True
    if isinstance(node, ast.Call) and len(node.args) == 1:
        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else None
        )
        if name in {"Path", "PurePath", "PurePosixPath", "str", "fspath"}:
            return _is_module_path(node.args[0])
    return False


def check_behavioral_verifier(source: str) -> None:
    """Parse generated tests without importing or executing repository code."""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise ValueError(
            f"Tasksmith behavioral verifier has invalid Python at line {error.lineno}: {error.msg}"
        ) from error
    inspect_modules = set()
    source_functions = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            inspect_modules.update(
                item.asname or item.name for item in node.names if item.name == "inspect"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "inspect":
            source_functions.update(
                item.asname or item.name
                for item in node.names
                if item.name in {"getsource", "getsourcelines"}
            )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        source_inspection = (
            isinstance(function, ast.Name) and function.id in source_functions
        ) or (
            isinstance(function, ast.Attribute)
            and function.attr in {"getsource", "getsourcelines"}
            and isinstance(function.value, ast.Name)
            and function.value.id in inspect_modules
        )
        reads_file = (isinstance(function, ast.Name) and function.id == "open") or (
            isinstance(function, ast.Attribute)
            and function.attr in {"open", "read_text", "read_bytes"}
        )
        # Only the module path itself (possibly wrapped as a Path/string). A
        # sibling fixture derived from that path is not implementation source.
        file_arguments = [*node.args, *(item.value for item in node.keywords if item.arg == "file")]
        if isinstance(function, ast.Attribute):
            file_arguments.append(function.value)
        module_file = reads_file and any(_is_module_path(value) for value in file_arguments)
        if source_inspection or module_file:
            raise ValueError(
                f"Tasksmith behavioral verifier reads implementation source at line {node.lineno}. "
                "Call the production behavior and assert its outputs or observable effects; "
                "do not match source text against a reference or known wrong probe."
            )
