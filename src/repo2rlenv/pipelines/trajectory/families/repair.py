"""repair: a check failed, the agent edited files, then the same check passed.

The task starts from the files as they were at the failure; the agent must make the check
pass (reward 1, else 0). Kept only if, in the task's image: the check fails the way the
trajectory recorded, the agent's original fix passes it (twice), doing nothing scores 0, and
rewriting the check's own files scores 0 (tests and build config are restored before grading).
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from ..docker import read_files_from_image, run_in_container
from ..task import AGENT_TOOLS, Family, Reject, Trial, task_toml
from ..trajectory import ShellRun, Trajectory

CHECK_COMMAND = re.compile(
    r"\b(pytest|python3?\s+(-m\s+(pytest|unittest)|\S+\.py)|node\s+(--test\s+)?\S+\.(m?js|cjs|ts)"
    r"|(npm|pnpm|yarn|bun)\s+(run\s+)?(test|build|lint|typecheck|check)\b|npx\s+(tsc|jest|vitest|eslint|tsx)"
    r"|\btsc\b|\bjest\b|\bvitest\b|cargo\s+(test|build|check)|go\s+(test|build|vet)|\bmake\b|\bruff\b|\bmypy\b)"
)
INSTALL_COMMAND = re.compile(
    r"\b(pip3?|uv\s+pip|npm|pnpm|yarn|bun|apt(-get)?|cargo)\s+(install|add|i)\b"
)
NETWORK_COMMAND = re.compile(r"\bcurl\b|\bwget\b|\bgit\b")
ENVIRONMENT_FAILURE = re.compile(
    r"command not found|No module named|Cannot find module|ENOTFOUND|ECONNREFUSED|"
    r"Permission denied|NameResolutionError|Max retries exceeded|getaddrinfo",
    re.I,
)
# Setup commands are replayed when building the image: scaffolds first (into an empty folder), then installs.
SCAFFOLD_COMMAND = re.compile(
    r"\b(npm|pnpm|yarn)\s+(create|init)\b|\bnpx\s+(--yes\s+)?create-|\buv\s+init|cargo\s+(new|init)"
)
SETUP_COMMAND = re.compile(
    SCAFFOLD_COMMAND.pattern + r"|\b(npm|pnpm|yarn)\s+(install|i|uninstall|add)\b|"
    r"python3?\s+-m\s+venv|\buv\s+(venv|pip\s+install|add|sync)|\bpip3?\s+install|"
    r"-m\s+pip\s+install|go\s+mod\s+init|^\s*(mkdir|rm)\s"
)
NODE_COMMAND = re.compile(r"\b(npm|npx|node|yarn|pnpm|corepack|tsc|vite|bun)\b")
TEST_FILE = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|[._-](test|spec)\.[a-z]+$|(^|/)test_[^/]+\.py$"
)
CHECK_CONFIG_FILE = re.compile(
    r"(^|/)(package(-lock)?\.json|tsconfig[^/]*\.json|vite\.config\.[a-z]+|eslint\.config\.[a-z]+|"
    r"\.eslintrc[^/]*|jest\.config\.[a-z]+|vitest\.config\.[a-z]+|pytest\.ini|pyproject\.toml|"
    r"setup\.cfg|tox\.ini|conftest\.py|Makefile)$"
)
ERROR_LINE = re.compile(r"error|FAIL|failed|Traceback|assert|Exception|TS\d{4}", re.I)
ANSI_COLOR = re.compile(r"\x1b\[[0-9;]*m")

TEST_SH = """#!/usr/bin/env bash
# Restore the check's own files, run the check, reward 1 if it exits 0.
mkdir -p /logs/verifier
[ -d /tests/protected ] && cp -a /tests/protected/. {root}/
cd {cwd} && timeout 600 bash -lc {command} > /logs/verifier/check.log 2>&1
[ $? -eq 0 ] && echo 1 > /logs/verifier/reward.txt || echo 0 > /logs/verifier/reward.txt
"""
TAMPER_WITH_CHECK = r"""cd "{root}"
[ -f package.json ] && node -e 'const p=require("./package.json");for(const k in p.scripts||{{}})p.scripts[k]="true";require("fs").writeFileSync("package.json",JSON.stringify(p))'
find . -path ./node_modules -prune -o \( -name 'test_*.py' -o -name '*.test.*' \) -print | while read f; do : > "$f"; done
true"""
INSTRUCTION = """You are working on a project in `{root}`. This check currently fails:

```
cd {cwd} && {command}
```

Its output:

```
{failure_output}
```

Fix the project so that the check succeeds. It will be run exactly as shown above.

The user's requests that led to this state:

{user_requests}

Dependencies are installed and there is no internet access. These files are part of the check
and are restored before grading: {protected_files}. Make the code correct rather than weakening the check.
"""


@dataclass
class RepairCandidate:
    trajectory: Trajectory
    failing_run: ShellRun
    passing_run: ShellRun
    project_root: str
    files_at_failure: dict[str, str]  # relative path -> content
    files_at_success: dict[str, str]
    changed_files: list[str]
    setup_commands: list[tuple[str, str | None]]  # (command, cwd) that succeeded before the failure
    protected_files: dict[str, str] = field(default_factory=dict)

    @property
    def cwd(self) -> str:
        return self.passing_run.cwd or self.project_root


def same_check(command_a: str, command_b: str) -> bool:
    """Equal modulo a leading `cd DIR &&`, `2>&1`, a trailing `| tail -n N`, and whitespace."""

    def normalize(command: str) -> str:
        command = re.sub(r"^\s*cd\s+(\"[^\"]*\"|'[^']*'|\S+)\s*(&&|;)\s*", "", command.strip())
        command = re.sub(r"\s*2>&1|\s*\|\s*(tail|head)(\s+-n)?\s+-?\d+\s*$", "", command)
        return " ".join(command.split())

    return normalize(command_a) == normalize(command_b)


def is_fixable_failure(run: ShellRun) -> bool:
    return (
        run.exit_code not in (None, 0)
        and bool(CHECK_COMMAND.search(run.command))
        and not INSTALL_COMMAND.search(run.command)
        and not NETWORK_COMMAND.search(run.command)
        and not ENVIRONMENT_FAILURE.search(run.output)
    )


def find_project_root(run: ShellRun, files: dict[str, str]) -> str | None:
    """The shell's working directory, else the deepest folder holding every rebuilt file."""
    if run.cwd and run.cwd.startswith("/"):
        return posixpath.normpath(run.cwd)
    folders = [posixpath.dirname(path) for path in files if path.startswith("/")]
    return posixpath.commonpath(folders) if folders else None


def relative_to(root: str, files: dict[str, str]) -> dict[str, str]:
    return {
        posixpath.relpath(path, root): content
        for path, content in files.items()
        if path.startswith(root + "/")
    }


def error_lines(output: str) -> set[str]:
    """The error lines of a failing run, with numbers normalized away (to compare two failures)."""
    text = ANSI_COLOR.sub("", output.split("\nOutput:\n", 1)[-1])
    return {
        re.sub(r"\d+(\.\d+)?", "N", " ".join(line.split()))[:160]
        for line in text.splitlines()
        if ERROR_LINE.search(line) and len(line.strip()) > 8
    }


def is_check_file(path: str) -> bool:
    return bool(CHECK_CONFIG_FILE.search(path) or TEST_FILE.search(path))


class Repair(Family):
    name = "repair"

    def find_candidates(self, trajectory: Trajectory) -> list[RepairCandidate]:
        runs, _ = trajectory.replay()
        candidates, used_failures = [], set()
        for failure_index, failing_run in enumerate(runs):
            if not is_fixable_failure(failing_run):
                continue
            for later_index in range(failure_index + 1, len(runs)):
                later_run = runs[later_index]
                if (
                    not same_check(later_run.command, failing_run.command)
                    or later_run.exit_code is None
                ):
                    continue
                if (
                    later_run.exit_code != 0
                ):  # still failing: the task starts from the latest failure
                    failure_index, failing_run = later_index, later_run
                    continue
                candidate = self.pair_to_candidate(trajectory, runs, failure_index, later_index)
                if candidate and failure_index not in used_failures:
                    candidates.append(candidate)
                    used_failures.add(failure_index)
                break
        return candidates

    def pair_to_candidate(
        self, trajectory, runs, failure_index, success_index
    ) -> RepairCandidate | None:
        failing_run, passing_run = runs[failure_index], runs[success_index]
        if any(
            INSTALL_COMMAND.search(run.command) for run in runs[failure_index + 1 : success_index]
        ):
            return None  # fixed by installing something, not by editing code
        root = find_project_root(failing_run, passing_run.files_before)
        if not root or "\\" in root or re.match(r"^/?[A-Za-z]:", root):
            return None  # unknown or Windows root: can't rebuild in a Linux container
        before = relative_to(root, failing_run.files_before)
        after = relative_to(root, passing_run.files_before)
        changed = sorted(
            path for path in set(before) | set(after) if before.get(path) != after.get(path)
        )
        if not changed or all(TEST_FILE.search(path) for path in changed):
            return None  # nothing changed, or only the tests did
        setup = [
            (run.command, run.cwd)
            for run in runs[:failure_index]
            if run.exit_code == 0 and SETUP_COMMAND.search(run.command)
        ]
        return RepairCandidate(
            trajectory, failing_run, passing_run, root, before, after, changed, setup
        )

    def task_id(self, c: RepairCandidate) -> str:
        fix = repr(sorted((path, c.files_at_success.get(path)) for path in c.changed_files))
        return f"{c.trajectory.short_id()}-{hashlib.sha256(fix.encode()).hexdigest()[:8]}"

    def prepare(self, c: RepairCandidate) -> None:
        if any(CHECK_CONFIG_FILE.search(path) for path in c.changed_files):
            raise Reject(
                "fix_changes_check_config"
            )  # can't protect the check if the fix must edit it

    def environment_files(self, c: RepairCandidate) -> dict[str, str]:
        all_commands = (
            c.passing_run.command + " " + " ".join(command for command, _ in c.setup_commands)
        )

        def run_step(command: str, cwd: str) -> str:
            return f"RUN mkdir -p {shlex.quote(cwd)} && cd {shlex.quote(cwd)} && bash -lc {shlex.quote(command)}"

        dockerfile = [
            f"FROM {'node:22-bookworm' if NODE_COMMAND.search(all_commands) else 'python:3.12-bookworm'}",
            AGENT_TOOLS,
            # npm resolves package versions as of the trajectory's date
            f"ENV CI=1 npm_config_yes=true NPM_CONFIG_BEFORE={c.trajectory.date_at(c.failing_run.step_id)}",
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
            f"COPY before/ {c.project_root}/",  # installs must not rewrite the code under test
        ]
        return {
            "Dockerfile": "\n".join(dockerfile) + "\n",
            **{f"before/{path}": content for path, content in c.files_at_failure.items()},
        }

    def check_in_image(self, c: RepairCandidate, image: str, scratch_dir: Path) -> None:
        exit_code, output = run_in_container(
            image,
            f"cd {shlex.quote(c.cwd)} && timeout 600 bash -lc {shlex.quote(c.passing_run.command)}",
        )
        if exit_code == 0:
            raise Reject("check_passes_before_fix")
        recorded_errors = error_lines(c.failing_run.output)
        if recorded_errors and not recorded_errors & error_lines(output):
            raise Reject("check_fails_differently", sorted(error_lines(output))[:3])
        # Protect tests and build config as found in the image: scaffolds write files no tool call shows.
        found_in_image = read_files_from_image(image, c.project_root, is_check_file)
        c.protected_files = {
            path: content
            for path, content in {**found_in_image, **c.files_at_failure}.items()
            if is_check_file(path) and path not in c.changed_files
        }

    def task_files(self, c: RepairCandidate) -> dict[str, str]:
        root = shlex.quote(c.project_root)
        deleted_files = [path for path in c.changed_files if path not in c.files_at_success]
        failure_output = ANSI_COLOR.sub("", c.failing_run.output.split("\nOutput:\n", 1)[-1])
        user_requests = [
            f"> {text[:500]}"
            for step_id, text in c.trajectory.user_messages()
            if step_id < c.failing_run.step_id
        ]
        protected = sorted(c.protected_files)
        solve_sh = (
            "#!/usr/bin/env bash\nset -e\n# The original agent's fix.\n"
            f"cp -a /solution/files/. {root}/\n"
            + "".join(
                f"rm -f {shlex.quote(posixpath.join(c.project_root, path))}\n"
                for path in deleted_files
            )
        )
        return {
            "instruction.md": INSTRUCTION.format(
                root=c.project_root,
                cwd=c.cwd,
                command=c.passing_run.command,
                failure_output="\n".join(failure_output.splitlines()[-60:]).replace("```", "'''"),
                user_requests="\n>\n".join(user_requests[-4:]) or "> (not recorded)",
                protected_files=", ".join(f"`{path}`" for path in protected) or "(none)",
            ),
            "tests/test.sh": TEST_SH.format(
                root=root, cwd=shlex.quote(c.cwd), command=shlex.quote(c.passing_run.command)
            ),
            **{f"tests/protected/{path}": content for path, content in c.protected_files.items()},
            "solution/solve.sh": solve_sh,
            **{
                f"solution/files/{path}": c.files_at_success[path]
                for path in c.changed_files
                if path in c.files_at_success
            },
            "task.toml": task_toml(
                self.name,
                self.task_id(c),
                f"Make `{c.passing_run.command}` pass",
                {
                    "source_trace": c.trajectory.source,
                    "source_model": c.trajectory.model,
                    "failing_step_id": c.failing_run.step_id,
                    "passing_step_id": c.passing_run.step_id,
                    "check_command": c.passing_run.command,
                    "changed_files": c.changed_files,
                    "protected_files": protected,
                    "trace_date": c.trajectory.date_at(c.failing_run.step_id),
                },
                cpus=2,
                memory_mb=4096,
            ),
        }

    def trials(self, c: RepairCandidate, scratch_dir: Path) -> tuple[list[Trial], dict]:
        return [
            Trial("original_fix", "bash /solution/solve.sh", "1"),
            Trial("original_fix_again", "bash /solution/solve.sh", "1"),  # catches flaky checks
            Trial("do_nothing", "true", "0"),
            Trial("tamper_with_check", TAMPER_WITH_CHECK.format(root=c.project_root), "0"),
        ], {}

    def summary(self, c: RepairCandidate) -> dict:
        return {"check_command": c.passing_run.command, "changed_files": c.changed_files}
