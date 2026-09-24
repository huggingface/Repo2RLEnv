"""Remove selected bodies while preserving interfaces and unrelated source."""

from __future__ import annotations

import ast
import hashlib
import json
import tomllib
from pathlib import Path

from repo2rlenv.emitter.bundle import relative_asset_path
from repo2rlenv.pipelines.recipes.codemidas.models import Feature, Verifier


def reconstruction_identity(repo: str, revision: str, defective: dict[str, bytes]) -> str:
    """Identify the missing implementation, independently of the authoring anchor."""
    value = {
        "repository": repo,
        "revision": revision,
        "files": {path: hashlib.sha256(content).hexdigest() for path, content in defective.items()},
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def existing_reconstructions(directory: Path) -> set[str]:
    identities = set()
    for config in directory.glob("*/task.toml"):
        metadata = tomllib.loads(config.read_text()).get("metadata", {}).get("repo2env", {})
        if metadata.get("recipe") != "codemidas":
            continue
        task = config.parent
        reference = task / "solution/reference"
        defective = {
            path.relative_to(reference).as_posix(): (
                task / "environment/source" / path.relative_to(reference)
            ).read_bytes()
            for path in reference.rglob("*.py")
        }
        identities.add(
            reconstruction_identity(metadata["repository"], metadata["source_revision"], defective)
        )
    return identities


def symbols(source: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    result = {}
    ambiguous = set()

    def visit(nodes, prefix=""):
        for node in nodes:
            name = prefix + getattr(node, "name", "")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if name in result:
                    result.pop(name)
                    ambiguous.add(name)
                elif name not in ambiguous:
                    result[name] = node
            elif isinstance(node, ast.ClassDef):
                visit(node.body, name + ".")

    visit(ast.parse(source).body)
    return result


def remove_bodies(source: str, names: list[str]) -> str:
    available = symbols(source)
    lines = source.splitlines(keepends=True)
    spans = []
    for name in names:
        if name not in available:
            raise ValueError(f"Unknown implementation boundary: {name}")
        node = available[name]
        first = node.body[0]
        # A one-line definition needs replacement after its header colon.
        if first.lineno == node.lineno:
            raise ValueError("Select a multiline implementation body")
        indent = " " * first.col_offset
        spans.append(
            (
                first.lineno - 1,
                node.end_lineno,
                indent + 'raise NotImplementedError("Implement the requested behavior")\n',
            )
        )
    for first, last, replacement in sorted(spans, reverse=True):
        lines[first:last] = [replacement]
    result = "".join(lines)
    ast.parse(result)
    return result


def implementation_pair(base: Path, feature: Feature, source_paths: list[str]):
    grouped = {}
    for symbol in feature.symbols:
        path = relative_asset_path("environment/" + symbol.path).relative_to("environment")
        if path.suffix != ".py" or not any(
            path == relative_asset_path("environment/" + root).relative_to("environment")
            or relative_asset_path("environment/" + root).relative_to("environment") in path.parents
            for root in source_paths
        ):
            raise ValueError("Implementation must belong to configured Python source roots")
        grouped.setdefault(symbol.path, []).append(symbol.qualified_name)
    reference, defective = {}, {}
    for path, names in grouped.items():
        local = base / path
        if local.is_symlink() or not local.resolve().is_relative_to(base.resolve()):
            raise ValueError("Implementation path escapes its snapshot")
        reference[path] = local.read_bytes()
        defective[path] = remove_bodies(reference[path].decode(), names).encode()
    return defective, reference


def validate_assertions(feature: Feature, verifier: Verifier):
    try:
        tree = ast.parse(verifier.test_code)
    except SyntaxError as exc:
        raise ValueError(f"Generated verifier is not valid Python: {exc}") from exc
    tests = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }
    mapped = [item.test for item in verifier.assertions]
    if set(mapped) != tests or len(mapped) != len(set(mapped)):
        raise ValueError("Map each top-level pytest test exactly once")
    required = {item.id for item in feature.requirements}
    covered = {item for test in verifier.assertions for item in test.requirements}
    if covered != required:
        raise ValueError("Map all and only the declared behavioral requirements")
    if not any(isinstance(node, ast.Assert) for node in ast.walk(tree)):
        raise ValueError("The verifier must contain behavioral assertions")
