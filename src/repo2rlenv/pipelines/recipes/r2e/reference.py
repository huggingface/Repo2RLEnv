"""Create private differential-test bindings; never placed in the learner image."""

from __future__ import annotations

import ast
import json


def verifier_files(candidate: dict, source: bytes, test: str) -> dict[str, bytes]:
    name, module = candidate["function_name"], candidate["module"]
    tree = ast.parse(test)
    identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    tests = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]
    if not tests or name not in identifiers or "reference_" + name not in identifiers:
        raise ValueError("R2E tests must compare the target and its private reference")
    insertion = 0
    for index, node in enumerate(tree.body):
        if (
            index == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ) or (isinstance(node, ast.ImportFrom) and node.module == "__future__"):
            insertion = index + 1
        else:
            break
    tree.body[insertion:insertion] = ast.parse(
        "import sys\nsys.path.insert(0, '/workspace')\n"
    ).body
    test = ast.unparse(tree) + "\n"
    reference_name = ".".join(filter(None, (module.rpartition(".")[0], "_r2e_reference")))
    shim = f"""import importlib.util
import json
import sys
from pathlib import Path
from {module} import {name}

_spec = importlib.util.spec_from_file_location({json.dumps(reference_name)}, '/workspace/.r2e_reference.py')
_reference = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _reference
_spec.loader.exec_module(_reference)
_observations = []
def reference_{name}(*args, **kwargs):
    value = _reference.{name}(*args, **kwargs)
    if len(_observations) < 20:
        plain = isinstance(value, (str, int, float, bool, tuple, list, dict, type(None)))
        _observations.append({{'argument_types': [type(arg).__name__ for arg in args],
                              'output_type': type(value).__name__,
                              'input_repr': repr((args, kwargs))[:2000],
                              'output_repr': repr(value)[:2000] if plain else None}})
        Path('/tmp/observations.json').write_text(json.dumps(_observations))
    return value
"""
    return {
        "fut_module.py": shim.encode(),
        ".r2e_reference.py": source,
        "tests/test_r2e_generated.py": test.encode(),
    }
