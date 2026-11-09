"""extend: implement the user's next request in the project the agent had built so far.

The task starts from the files at the end of request k (dependencies installed) and gives the
agent request k+1. The reference is the agent's project at the end of request k+1, whose last
Python check passed. The LLM writes a minimal contract and candidate tests; calibration in the
image sorts them like SWE-bench: a "new" test passes on the reference (twice) and fails on the
start state; an "existing" test passes on both. Kept tests must catch most mutants of the changed
code. Reward = fraction of new tests passed, or 0 if any existing test fails.
"""

from __future__ import annotations

import json
import posixpath
import shlex
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
from .build import NON_PYTHON_SOURCE, PYTHON_CHECK, keep_only_tests
from .repair import INSTALL_COMMAND, SCAFFOLD_COMMAND, SETUP_COMMAND, TEST_FILE

MINIMUM_NEW_TESTS, MINIMUM_MUTANT_KILL_RATE = 5, 0.5
MAXIMUM_PROMPT_CHARS = 150_000
CALIBRATION_SCRIPT = Path(__file__).resolve().parents[1] / "assets" / "calibrate.py"

SYSTEM_PROMPT = """You write hidden tests for a feature-request RL task.

An agent receives an existing Python project in {root} (shown as <before>), the user's earlier
requests, and the user's next request, which it must implement. You are shown how the original
agent implemented it (<after>: the files it changed or added). Write:

1. contract: markdown with only the interface details your tests rely on that the request does
   not already fix: new module/file paths, function and class names with signatures, CLI flags,
   input/output formats. Do not describe the implementation. Keep it short; it may be empty if
   the tests only use the existing interface and the request's own wording. Anything a test
   asserts (message substrings, keys, exit codes) must be pinned down here or in the request.
2. tests: one pytest module with 10-20 `test_*` functions checking the behaviour the next request
   asks for, plus 2-5 `test_existing_*` functions checking behaviour the project already had and
   must keep. Use the contract and the existing public interface only. Locate code with
   WS = os.environ.get("WORKSPACE", "{root}") on sys.path (and WS/src for a src layout); run CLIs
   with subprocess and sys.executable, cwd=WS, timeout=60. Import the project inside each test
   (or a helper it calls), never at module level: a name the agent got wrong must fail only the
   tests that use it, and the test_existing_* tests must run on the project as it was before.
   Use tmp_path, no network, standard library + pytest + the project's own dependencies only,
   deterministic. Test what the request requires, not incidental choices (help text, log
   wording, private helpers)."""

DOCKERFILE_HEAD = [
    "FROM python:3.12-bookworm",
    AGENT_TOOLS,
    "RUN pip install --no-cache-dir pytest==8.4.2 pytest-timeout==2.4.0",
]
TEST_SH = """#!/usr/bin/env bash
# Run the hidden tests; reward = fraction of new tests passed, 0 if an existing behaviour broke.
mkdir -p /logs/verifier
cd /tmp && python -m pytest -q -p no:cacheprovider --timeout=60 --junitxml=/logs/verifier/junit.xml \\
  /tests/test_hidden.py > /logs/verifier/pytest.log 2>&1
python - <<'PY'
import json, xml.etree.ElementTree as ET
grading = json.load(open("/tests/grading.json"))
passed = set()
try:
    for case in ET.parse("/logs/verifier/junit.xml").getroot().iter("testcase"):
        class_name = case.get("classname", "").split(".")[-1]
        name = (class_name + "::" if class_name.startswith("Test") else "") + case.get("name")
        if not any(child.tag in ("failure", "error", "skipped") for child in case):
            passed.add(name)
except Exception:
    pass
new = grading["new"]
reward = 0.0 if not set(grading["existing"]) <= passed else len(passed & set(new)) / len(new)
open("/logs/verifier/reward.txt", "w").write(f"{reward:.4f}\\n")
PY
"""
INSTRUCTION = """You are working on an existing Python project in `{root}`.

## Earlier requests (already implemented)

{earlier_requests}

## Next request

{request}

## Interface notes

Hidden tests check the new behaviour through this interface (and the project's existing one), and
check that existing behaviour still works. Follow these names exactly; everything else is up to you.

{contract}

## Environment

Python 3.12 with the project's dependencies installed (pytest too). No internet. Each new-behaviour
test passed earns partial credit; breaking existing behaviour scores 0.
"""


class GeneratedTests(BaseModel):
    contract: str
    tests: str = Field(description="a single pytest module, test_hidden.py")


@dataclass
class ExtendCandidate:
    trajectory: Trajectory
    request_index: int  # index (in trajectory.user_messages()) of the request to implement
    project_root: str
    files_before: dict[str, str]  # relative path -> content, at the end of the previous request
    files_after: dict[str, str]  # ... at the end of this request (the reference)
    setup_commands: list[tuple[str, str | None]]
    generated: GeneratedTests | None = None
    new_tests: set[str] = field(default_factory=set)
    existing_tests: set[str] = field(default_factory=set)
    mutant_kill_rate: float | None = None

    @property
    def request(self) -> str:
        return self.trajectory.user_messages()[self.request_index][1]

    @property
    def changed_files(self) -> list[str]:
        return sorted(
            path
            for path in set(self.files_before) | set(self.files_after)
            if self.files_before.get(path) != self.files_after.get(path)
        )


def project_files(root: str, files: dict[str, str]) -> dict[str, str]:
    return {
        posixpath.relpath(path, root): content
        for path, content in files.items()
        if path.startswith(root + "/")
    }


def render_files(files: dict[str, str]) -> str:
    return "\n\n".join(
        f"### {path}\n```\n{content}\n```" for path, content in sorted(files.items())
    )


class Extend(Family):
    name = "extend"

    def find_candidates(self, trajectory: Trajectory) -> list[ExtendCandidate]:
        """Each later request whose Python project existed before it and passed its checks after it."""
        user_messages = trajectory.user_messages()
        candidates = []
        for index in range(1, len(user_messages)):
            start_step = user_messages[index][0]
            end_step = user_messages[index + 1][0] if index + 1 < len(user_messages) else None
            runs_before, fs_before = trajectory.replay(until_step=start_step)
            runs_after, fs_after = trajectory.replay(until_step=end_step)
            checks = [
                run for run in runs_after[len(runs_before) :] if PYTHON_CHECK.search(run.command)
            ]
            python_files = [path for path in fs_after.files if path.endswith(".py")]
            if (
                fs_after.lost_files
                or not checks
                or checks[-1].exit_code != 0
                or len(python_files) < 2
            ):
                continue
            root = posixpath.commonpath([posixpath.dirname(path) for path in python_files])
            root = next(
                (run.cwd for run in runs_after if run.cwd and root.startswith(run.cwd)), root
            )
            before, after = (
                project_files(root, fs_before.files),
                project_files(root, fs_after.files),
            )
            if (
                sum(path.endswith(".py") for path in before) < 2
                or any(NON_PYTHON_SOURCE.search(path) for path in after)
                or any(path.startswith(root + "/") for path in fs_before.lost_files)
                or not any(
                    path.endswith(".py")
                    and not TEST_FILE.search(path)
                    and before.get(path) != after.get(path)
                    for path in after
                )
            ):
                continue  # no existing Python project, not Python, or no source change ("add tests for ...")
            setup = [
                (run.command, run.cwd)
                for run in runs_before
                if run.exit_code == 0 and SETUP_COMMAND.search(run.command)
            ]
            setup += [
                (run.command, run.cwd)
                for run in runs_after[len(runs_before) :]
                if run.exit_code == 0 and INSTALL_COMMAND.search(run.command)
            ]  # offline: preinstall what it added
            candidates.append(ExtendCandidate(trajectory, index, root, before, after, setup))
        return candidates

    def task_id(self, c: ExtendCandidate) -> str:
        return f"{c.trajectory.short_id()}-extend-{c.request_index + 1}"

    def prepare(self, c: ExtendCandidate) -> None:
        changed = {path: c.files_after[path] for path in c.changed_files if path in c.files_after}
        deleted = [path for path in c.changed_files if path not in c.files_after]
        earlier = "\n\n".join(text for _, text in c.trajectory.user_messages()[: c.request_index])
        prompt = (
            f"<earlier_requests>\n{earlier}\n</earlier_requests>\n\n<next_request>\n{c.request}\n</next_request>\n\n"
            f"<before>\n{render_files(c.files_before)}\n</before>\n\n<after>\n{render_files(changed)}\n"
            f"{'Deleted: ' + ', '.join(deleted) if deleted else ''}\n</after>"
        )
        if len(prompt) > MAXIMUM_PROMPT_CHARS:
            raise Reject("project_too_large", len(prompt))
        c.generated = ask(SYSTEM_PROMPT.format(root=c.project_root), prompt, GeneratedTests)
        if c.generated is None:
            raise Reject("llm_declined")

    def environment_files(self, c: ExtendCandidate) -> dict[str, str]:
        def run_step(command: str, cwd: str) -> str:
            return f"RUN mkdir -p {shlex.quote(cwd)} && cd {shlex.quote(cwd)} && bash -lc {shlex.quote(command)}"

        dockerfile = [
            *DOCKERFILE_HEAD,
            f"ENV WORKSPACE={c.project_root}",
            f"WORKDIR {c.project_root}",
            *[
                run_step(command, cwd or posixpath.dirname(c.project_root))
                for command, cwd in c.setup_commands
                if SCAFFOLD_COMMAND.search(command)
            ],
            f"COPY before/ {c.project_root}/",
            *[
                run_step(command, cwd or c.project_root)
                for command, cwd in c.setup_commands
                if not SCAFFOLD_COMMAND.search(command)
            ],
            f"COPY before/ {c.project_root}/",  # installs must not rewrite the starting code
        ]
        return {
            "Dockerfile": "\n".join(dockerfile) + "\n",
            **{f"before/{path}": content for path, content in c.files_before.items()},
        }

    def check_in_image(self, c: ExtendCandidate, image: str, scratch_dir: Path) -> None:
        """Sort candidate tests into new (fail-to-pass) and existing (pass-to-pass); check they catch mutants."""
        changed_python = {
            path: c.files_after[path]
            for path in c.changed_files
            if path in c.files_after and path.endswith(".py")
        }
        variants = {"after": c.files_after, "after_again": c.files_after, "before": c.files_before}
        variants |= {
            f"mutant_{index:02d}": {**c.files_after, **mutant}
            for index, mutant in enumerate(make_mutants(changed_python))
        }
        for variant_name, files in variants.items():
            (scratch_dir / "variants" / variant_name).mkdir(parents=True)
            write_files(scratch_dir / "variants" / variant_name, files)
        write_files(
            scratch_dir,
            {
                "test_hidden.py": c.generated.tests,
                "known_files.json": json.dumps(sorted(set(c.files_before) | set(c.files_after))),
            },
        )
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

        stable = {
            test
            for test in outcomes.get("after", {})
            if passes("after", test) and passes("after_again", test)
        }
        if not stable <= set(
            outcomes.get("before", {})
        ):  # the module itself failed to load before the request
            raise Reject(
                "tests_not_collected_before", sorted(stable - set(outcomes.get("before", {})))[:3]
            )
        c.new_tests = {test for test in stable if not passes("before", test)}
        c.existing_tests = stable - c.new_tests
        mutants = [variant for variant in outcomes if variant.startswith("mutant_")]
        caught = [
            mutant
            for mutant in mutants
            if any(not passes(mutant, test) for test in c.new_tests | c.existing_tests)
        ]
        c.mutant_kill_rate = round(len(caught) / len(mutants), 3) if mutants else None
        if len(c.new_tests) < MINIMUM_NEW_TESTS or (
            c.mutant_kill_rate is not None and c.mutant_kill_rate < MINIMUM_MUTANT_KILL_RATE
        ):
            raise Reject(
                "weak_tests",
                {
                    "new_tests": len(c.new_tests),
                    "existing_tests": len(c.existing_tests),
                    "mutant_kill_rate": c.mutant_kill_rate,
                },
            )

    def task_files(self, c: ExtendCandidate) -> dict[str, str]:
        root = shlex.quote(c.project_root)
        deleted = [path for path in c.changed_files if path not in c.files_after]
        earlier = [
            f"> {' '.join(text[:600].split())}"
            for _, text in c.trajectory.user_messages()[: c.request_index]
        ]
        solve_sh = (
            "#!/usr/bin/env bash\nset -e\n# The original agent's implementation of the request.\n"
            f"cp -a /solution/files/. {root}/\n"
            + "".join(
                f"rm -f {shlex.quote(posixpath.join(c.project_root, path))}\n" for path in deleted
            )
        )
        return {
            "instruction.md": INSTRUCTION.format(
                root=c.project_root,
                earlier_requests="\n>\n".join(earlier[-4:]),
                request=c.request,
                contract=c.generated.contract.strip() or "(none)",
            ),
            "tests/test_hidden.py": keep_only_tests(
                c.generated.tests, c.new_tests | c.existing_tests
            ),
            "tests/grading.json": json.dumps(
                {"new": sorted(c.new_tests), "existing": sorted(c.existing_tests)}, indent=1
            ),
            "tests/test.sh": TEST_SH,
            "solution/solve.sh": solve_sh,
            **{
                f"solution/files/{path}": c.files_after[path]
                for path in c.changed_files
                if path in c.files_after
            },
            "task.toml": task_toml(
                self.name,
                self.task_id(c),
                c.request,
                {
                    "source_trace": c.trajectory.source,
                    "source_model": c.trajectory.model,
                    "request_index": c.request_index,
                    "changed_files": c.changed_files,
                    "new_tests": len(c.new_tests),
                    "existing_tests": len(c.existing_tests),
                    "mutant_kill_rate": c.mutant_kill_rate,
                    "tests_written_by": llm.MODEL,
                },
                cpus=2,
                memory_mb=4096,
                agent_timeout_sec=2400.0,
            ),
        }

    def trials(self, c: ExtendCandidate, scratch_dir: Path) -> tuple[list[Trial], dict]:
        return [
            Trial("reference", "bash /solution/solve.sh", "1.0000"),
            Trial("reference_again", "bash /solution/solve.sh", "1.0000"),
            Trial("do_nothing", "true", "0.0000"),
            Trial("delete_project", f"rm -rf {shlex.quote(c.project_root)}/*", "0.0000"),
        ], {}

    def summary(self, c: ExtendCandidate) -> dict:
        return {
            "request": c.request[:200],
            "new_tests": len(c.new_tests),
            "existing_tests": len(c.existing_tests),
            "mutant_kill_rate": c.mutant_kill_rate,
        }
