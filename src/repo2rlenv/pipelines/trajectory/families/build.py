"""build: build a project from the user's request; hidden tests give partial credit.

The reference is the agent's Python project at the end of the trajectory's first request.
Claude writes an interface contract, a behaviour-free stub and candidate tests. A test is kept
only if it passes on the reference (twice) and fails on both the stub and an empty workspace;
together the kept tests must catch most mutants of the reference. Reward = fraction passed.
"""

from __future__ import annotations

import ast
import json
import posixpath
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from .. import llm
from ..docker import run_in_container
from ..llm import ask
from ..mutants import make_mutants
from ..task import AGENT_TOOLS, Family, Reject, Trial, task_toml, write_files
from ..trajectory import Trajectory

MINIMUM_TESTS, MINIMUM_MUTANT_KILL_RATE = 6, 0.6
CALIBRATION_SCRIPT = Path(__file__).resolve().parents[1] / "assets" / "calibrate.py"
NON_PYTHON_SOURCE = re.compile(r"\.(js|ts|tsx|jsx|go|rs)$")
PYTHON_CHECK = re.compile(r"\b(pytest|unittest|python3?\s+\S+\.py)")

SYSTEM_PROMPT = """You write hidden tests for a build-from-spec RL task.

A fresh agent receives only the user's request and your interface contract, and builds the
project from scratch in {root}. Your tests grade it with partial credit. You are shown a
reference implementation that satisfied the request.

1. contract: markdown with the minimum public interface the tests need: file/module paths
   (relative to {root}), signatures with types, CLI usage, input/output formats, exit codes.
   Anything a test asserts must be pinned down here or in the request.
2. stub_files: files with the contract's signatures and no behaviour (raise
   NotImplementedError; CLI entry points print nothing and exit 1).
3. tests: one pytest module with 12-20 independent `test_*` functions checking behaviour the
   request asks for, through the contract only. Locate code with
   WS = os.environ.get("WORKSPACE", "{root}") on sys.path (and WS/src for a src layout); run CLIs
   with subprocess and sys.executable, cwd=WS, timeout=60. Use tmp_path, no network, standard
   library + pytest only, deterministic. Test what the request requires, not incidental choices
   (help text, log wording). For error cases assert on more than the exit code."""

DOCKERFILE = (
    """FROM python:3.12-bookworm
"""
    + AGENT_TOOLS
    + """
RUN pip install --no-cache-dir pytest==8.4.2 pytest-timeout==2.4.0
ENV WORKSPACE={root}
WORKDIR {root}
"""
)
TEST_SH = """#!/usr/bin/env bash
# Run the hidden tests against the workspace; reward = fraction passed.
mkdir -p /logs/verifier
cd /tmp && python -m pytest -q -p no:cacheprovider --timeout=60 --junitxml=/logs/verifier/junit.xml \\
  /tests/test_hidden.py > /logs/verifier/pytest.log 2>&1
python - <<'PY'
import xml.etree.ElementTree as ET
passed = 0
try:
    for case in ET.parse("/logs/verifier/junit.xml").getroot().iter("testcase"):
        passed += not any(child.tag in ("failure", "error", "skipped") for child in case)
except Exception:
    pass
open("/logs/verifier/reward.txt", "w").write(f"{{min(passed, {total}) / {total}:.4f}}\\n")
PY
"""
INSTRUCTION = """Build the following project from scratch in `{root}` (it is currently empty).

## Request

{request}

## Interface contract

Hidden tests use only this interface, so follow it exactly. Everything else is up to you.

{contract}

## Environment

Python 3.12, standard library only (pytest is installed). No internet. Each hidden test passed earns partial credit.
"""


class GeneratedFile(BaseModel):
    path: str = Field(description="path relative to the project root")
    content: str


class GeneratedTests(BaseModel):
    contract: str
    stub_files: list[GeneratedFile]
    tests: str = Field(description="a single pytest module, test_hidden.py")


@dataclass
class BuildCandidate:
    trajectory: Trajectory
    project_root: str
    request: str
    reference_files: dict[str, str]  # relative path -> content
    generated: GeneratedTests | None = None
    kept_tests: set[str] = field(default_factory=set)
    mutant_kill_rate: float = 0.0


def keep_only_tests(test_module: str, kept_test_names: set[str]) -> str:
    """Remove test functions (and TestClass methods) whose pytest name isn't kept."""
    tree = ast.parse(test_module)

    def filter_body(body: list[ast.stmt], prefix: str = "") -> list[ast.stmt]:
        kept = []
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test"
            ):
                if prefix + node.name in kept_test_names:
                    kept.append(node)
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                node.body = filter_body(node.body, node.name + "::") or [ast.Pass()]
                kept.append(node)
            else:
                kept.append(node)
        return kept

    tree.body = filter_body(tree.body)
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n"


class Build(Family):
    name = "build"

    def find_candidates(self, trajectory: Trajectory) -> list[BuildCandidate]:
        """A self-contained Python project, finished (its last check passing) within the first request."""
        user_messages = trajectory.user_messages()
        if not user_messages:
            return []
        second_request = user_messages[1][0] if len(user_messages) > 1 else None
        runs, filesystem = trajectory.replay(until_step=second_request)
        python_files = [path for path in filesystem.files if path.endswith(".py")]
        checks = [run for run in runs if PYTHON_CHECK.search(run.command)]
        if (
            len(python_files) < 2
            or filesystem.lost_files
            or not checks
            or checks[-1].exit_code != 0
        ):
            return []
        root = posixpath.commonpath([posixpath.dirname(path) for path in python_files])
        root = next((run.cwd for run in runs if run.cwd and root.startswith(run.cwd)), root)
        files = {
            posixpath.relpath(path, root): content
            for path, content in filesystem.files.items()
            if path.startswith(root + "/")
        }
        if any(NON_PYTHON_SOURCE.search(path) for path in files):
            return []
        return [BuildCandidate(trajectory, root, user_messages[0][1], files)]

    def task_id(self, c: BuildCandidate) -> str:
        return f"{c.trajectory.short_id()}-build"

    def prepare(self, c: BuildCandidate) -> None:
        reference = "\n\n".join(
            f"### {path}\n```\n{content}\n```"
            for path, content in sorted(c.reference_files.items())
        )
        c.generated = ask(
            SYSTEM_PROMPT.format(root=c.project_root),
            f"<request>\n{c.request}\n</request>\n\n<reference>\n{reference}\n</reference>",
            GeneratedTests,
        )
        if c.generated is None:
            raise Reject("llm_declined")

    def environment_files(self, c: BuildCandidate) -> dict[str, str]:
        return {"Dockerfile": DOCKERFILE.format(root=c.project_root)}

    def check_in_image(self, c: BuildCandidate, image: str, scratch_dir: Path) -> None:
        """Run the candidate tests on the reference, the stub, an empty workspace and mutants."""
        variants = {
            "reference": c.reference_files,
            "reference_again": c.reference_files,
            "stub": self.stub_files(c),
            "empty": {},
        }
        variants |= {
            f"mutant_{index:02d}": files
            for index, files in enumerate(make_mutants(c.reference_files))
        }
        for variant_name, files in variants.items():
            (scratch_dir / "variants" / variant_name).mkdir(parents=True)
            write_files(scratch_dir / "variants" / variant_name, files)
        write_files(scratch_dir, {"test_hidden.py": c.generated.tests})
        shutil.copy(CALIBRATION_SCRIPT, scratch_dir / "calibrate.py")
        run_in_container(
            image,
            "python /calibration/calibrate.py",
            {scratch_dir: "/calibration"},
            timeout_seconds=3600,
        )
        results_file = scratch_dir / "results.json"
        if not results_file.exists():
            raise Reject("calibration_failed")
        outcomes = json.loads(results_file.read_text())  # {variant: {test name: outcome}}

        def passes(variant: str, test: str) -> bool:
            return outcomes.get(variant, {}).get(test) == "passed"

        all_tests = outcomes.get("reference", {})
        c.kept_tests = {
            test
            for test in all_tests
            if passes("reference", test)
            and passes("reference_again", test)
            and not passes("stub", test)
            and not passes("empty", test)
        }
        mutants = [variant for variant in outcomes if variant.startswith("mutant_")]
        caught = [
            mutant for mutant in mutants if any(not passes(mutant, test) for test in c.kept_tests)
        ]
        c.mutant_kill_rate = round(len(caught) / max(len(mutants), 1), 3)
        if len(c.kept_tests) < MINIMUM_TESTS or c.mutant_kill_rate < MINIMUM_MUTANT_KILL_RATE:
            raise Reject(
                "weak_tests",
                {"kept_tests": len(c.kept_tests), "mutant_kill_rate": c.mutant_kill_rate},
            )

    def stub_files(self, c: BuildCandidate) -> dict[str, str]:
        return {file.path: file.content for file in c.generated.stub_files}

    def task_files(self, c: BuildCandidate) -> dict[str, str]:
        return {
            "instruction.md": INSTRUCTION.format(
                root=c.project_root, request=c.request, contract=c.generated.contract.strip()
            ),
            "tests/test_hidden.py": keep_only_tests(c.generated.tests, c.kept_tests),
            "tests/test.sh": TEST_SH.format(total=len(c.kept_tests)),
            "solution/solve.sh": f"#!/usr/bin/env bash\nset -e\n# The original agent's implementation.\ncp -a /solution/files/. {c.project_root}/\n",
            **{f"solution/files/{path}": content for path, content in c.reference_files.items()},
            "task.toml": task_toml(
                self.name,
                self.task_id(c),
                c.request,
                {
                    "source_trace": c.trajectory.source,
                    "source_model": c.trajectory.model,
                    "hidden_tests": len(c.kept_tests),
                    "mutant_kill_rate": c.mutant_kill_rate,
                    "tests_written_by": llm.MODEL,
                },
                agent_timeout_sec=1800.0,
            ),
        }

    def trials(self, c: BuildCandidate, scratch_dir: Path) -> tuple[list[Trial], dict]:
        stub_dir = scratch_dir / "trial_stub"
        write_files(
            stub_dir, self.stub_files(c)
        )  # mounted for the trial only: never shipped with the task
        return [
            Trial("reference", "bash /solution/solve.sh", "1.0000"),
            Trial("stub", f"cp -a /stub/. {c.project_root}/", "0.0000"),
            Trial("empty_workspace", "true", "0.0000"),
        ], {stub_dir: "/stub:ro"}

    def summary(self, c: BuildCandidate) -> dict:
        return {
            "request": c.request[:200],
            "hidden_tests": len(c.kept_tests),
            "mutant_kill_rate": c.mutant_kill_rate,
        }
