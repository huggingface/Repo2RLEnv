"""Remote R2E preparation and bounded execution feedback, without model credentials."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import random
from importlib.resources import files
from pathlib import Path

from repo2rlenv.execution.lifecycle import save_record
from repo2rlenv.execution.python_repository import (
    TestInstrumentation,
    bootstrap_snapshot,
    test_image,
)
from repo2rlenv.pipelines.recipes.r2e.extract import dependency_slice, stub
from repo2rlenv.pipelines.recipes.r2e.reference import verifier_files
from repo2rlenv.quality.test_results import execution_contrast
from repo2rlenv.spec.input import RepoSpec
from repo2rlenv.spec.recipe_options import R2EOptions
from repo2rlenv.ui import console


def prepare(repo: RepoSpec, options: R2EOptions, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=False)
    boot, base = bootstrap_snapshot(repo, options, destination)
    healthy = test_image(boot.image_digest, options, destination / "healthy")
    if healthy.returncode or not healthy.passed:
        raise ValueError("Healthy repository must pass before R2E test generation")
    candidates, rejected = [], []
    paths = set()
    for relative in options.source_paths:
        path = base / relative
        paths.update([path] if path.is_file() else path.rglob("*.py"))
    for path in sorted(paths):
        source = path.read_text()
        relative = path.relative_to(base).as_posix()
        for node in ast.parse(source).body:
            if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
                continue
            code = ast.get_source_segment(source, node)
            if len(code) > 6000 or len(node.body) < 2 or not ast.get_docstring(node):
                continue
            try:
                context = dependency_slice(source, node.name)
            except ValueError as exc:
                rejected.append({"reason": str(exc), "function": node.name})
                continue
            identity = {
                "repo": repo.url,
                "ref": boot.ref,
                "path": relative,
                "function_name": node.name,
            }
            key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
            candidates.append(
                {
                    **identity,
                    "id": key,
                    "module": relative.removesuffix(".py").replace("/", "."),
                    "line": node.lineno,
                    "end_line": node.end_lineno,
                    "source": code,
                    "context": context,
                    "image_digest": boot.image_digest,
                }
            )
    random.Random(options.seed).shuffle(candidates)
    result = {
        "candidates": candidates[: options.max_candidates],
        "rejected": rejected,
        "attempted": min(len(candidates), options.max_candidates) + len(rejected),
    }
    save_record(destination / "generation.json", result)
    return result


def branch_evidence(report: dict, candidate: dict) -> dict:
    data = report.get("files", {}).get(candidate["path"], {})
    if not data:
        data = report.get("files", {}).get("/workspace/" + candidate["path"], {})
    first, last = candidate["line"], candidate["end_line"]
    covered = [arc for arc in data.get("executed_branches", []) if first <= arc[0] <= last]
    missed = [arc for arc in data.get("missing_branches", []) if first <= arc[0] <= last]
    executed = [line for line in data.get("executed_lines", []) if first < line <= last]
    total = len(covered) + len(missed)
    ratio = len(covered) / total if total else (1.0 if executed else 0.0)
    return {
        "branch_coverage": ratio,
        "covered_branches": covered,
        "missing_branches": missed,
        "executed_lines": executed,
    }


def evaluate(config: dict, options: R2EOptions, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=False)
    candidate = config["candidate"]
    base = Path(config["generation"]) / "base"
    source = (base / candidate["path"]).read_bytes()
    private = verifier_files(candidate, source, config["test_code"])
    replacements = {}
    for relative, content in private.items():
        if (base / relative).exists():
            raise ValueError("R2E verifier helper would replace an existing repository file")
        path = destination / "private" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        replacements[relative] = path
    healthy = test_image(
        candidate["image_digest"],
        options,
        destination / "healthy",
        replacements=replacements,
        instrumentation=TestInstrumentation(
            driver=Path(str(files(__package__).joinpath("coverage_driver.py"))),
            arguments=(candidate["path"],),
            outputs=("coverage.json", "observations.json"),
        ),
    )
    coverage_data = branch_evidence(
        json.loads((destination / "healthy/coverage.json").read_text()), candidate
    )
    result = {
        "reference_passed": healthy.returncode == 0 and bool(healthy.passed),
        **coverage_data,
        "observations": json.loads((destination / "healthy/observations.json").read_text()),
        "reference_log": (destination / "healthy/stdout.txt").read_text()[-24000:],
    }
    if result["reference_passed"] and result["branch_coverage"] >= options.min_branch_coverage:
        defective = destination / "stub.py"
        defective.write_text(stub(source.decode(), candidate["function_name"]))
        replacements[candidate["path"]] = defective
        broken = test_image(
            candidate["image_digest"], options, destination / "defective", replacements=replacements
        )
        result["contrast"] = execution_contrast(healthy, broken)
    save_record(destination / "evaluation.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    options = R2EOptions.model_validate(config["options"])
    if config.get("mode") == "evaluate":
        result = evaluate(config, options, args.output)
        console.json(
            {
                "reference_passed": result["reference_passed"],
                "branch_coverage": result["branch_coverage"],
            }
        )
    else:
        result = prepare(RepoSpec.model_validate(config["repo"]), options, args.output)
        console.json({"candidates": len(result["candidates"])})


if __name__ == "__main__":
    main()
