"""An ATIF trajectory, replayed as shell commands over a rebuilt filesystem.

ATIF fixes the structure of a trajectory but not the meaning of its tool calls. This module
maps the common tools onto two things:

  shell commands  Bash, exec_command, shell_command, ...      -> ShellRun(command, exit_code, ...)
  file changes    Write, Edit, MultiEdit, apply_patch, heredocs (and full Reads of unseen files)

and replays the file changes, so every shell command comes with the files as they were
just before it ran.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

SHELL_TOOLS = {"Bash", "bash", "exec_command", "shell_command", "shell", "run_terminal_command"}
WRITE_TOOLS = {"Write", "write", "write_file", "create_file"}
EDIT_TOOLS = {"Edit", "edit", "search_replace", "str_replace"}
READ_TOOLS = {"Read", "read", "read_file"}

EXIT_CODE_PATTERNS = [
    re.compile(r"Process exited with code (-?\d+)"),  # Codex exec_command
    re.compile(r"^Exit code:? (-?\d+)", re.M),  # Claude Code, Codex shell_command
    re.compile(r"process exit code was (-?\d+)"),  # fast-agent
]
TRAILING_EXIT_CODE = re.compile(
    r"Command exited with code (-?\d+)\s*$"
)  # pi, at the end of the output
HEREDOC_WRITE = re.compile(
    r"cat\s+>\s*(\"[^\"]+\"|'[^']+'|\S+)\s*<<-?\s*['\"]?(\w+)['\"]?\n(.*?)\n\2\s*$", re.S | re.M
)
LINE_NUMBER_PREFIX = re.compile(r"^\s*\d+(\t|→)", re.M)
INJECTED_CONTEXT = re.compile(
    r"<(?!file\b)([a-z_]+)[^>]*>.*?</\1>", re.S
)  # e.g. <environment_context>; <file> is pasted content


@dataclass
class ShellRun:
    step_id: int
    command: str
    cwd: str | None
    exit_code: int | None
    output: str
    files_before: dict[str, str]  # rebuilt files (absolute path -> content) just before it ran
    lost_files: set[str]  # files changed in ways we could not rebuild


def absolute(path: str, cwd: str | None) -> str:
    path = path.strip("\"'")
    if path.startswith("/") or not cwd:
        return posixpath.normpath(path)
    return posixpath.normpath(posixpath.join(cwd, path))


def leading_cd(command: str) -> str | None:
    """The directory of a leading `cd DIR &&`, if any."""
    match = re.match(r"^\s*cd\s+(\"[^\"]*\"|'[^']*'|\S+)\s*(&&|;)", command)
    return match.group(1).strip("\"'") if match else None


@dataclass
class VirtualFilesystem:
    files: dict[str, str] = field(default_factory=dict)
    lost_files: set[str] = field(default_factory=set)

    def write(self, path: str, content: str) -> None:
        self.files[path] = content
        self.lost_files.discard(path)

    def edit(self, path: str, old_text: str, new_text: str, replace_all: bool = False) -> None:
        current = self.files.get(path)
        if current is None or old_text not in current:
            self.lose(path)
        elif replace_all:
            self.files[path] = current.replace(old_text, new_text)
        else:
            self.files[path] = current.replace(old_text, new_text, 1)

    def seed_if_unseen(self, path: str, content: str) -> None:
        """A full read reveals a file the trajectory never wrote."""
        if path not in self.files and path not in self.lost_files:
            self.files[path] = content

    def lose(self, path: str) -> None:
        self.files.pop(path, None)
        self.lost_files.add(path)

    def apply_patch(self, patch: str, cwd: str | None) -> None:
        """Codex apply_patch format: *** Add File / Update File / Delete File, optional *** Move to."""
        parts = re.split(r"^\*\*\* (Add File|Update File|Delete File): (.+)$", patch, flags=re.M)
        for action, path, body in zip(parts[1::3], parts[2::3], parts[3::3], strict=False):
            path = absolute(path.strip(), cwd)
            body = re.sub(r"^\*\*\* End (Patch|of File)\s*$", "", body, flags=re.M)
            if action == "Add File":
                self.write(
                    path,
                    "".join(line[1:] + "\n" for line in body.splitlines() if line.startswith("+")),
                )
            elif action == "Delete File":
                self.files.pop(path, None)
            else:
                move_to = re.search(r"^\*\*\* Move to: (.+)$", body, flags=re.M)
                self._apply_hunks(path, re.sub(r"^\*\*\* Move to: .+$", "", body, flags=re.M))
                if move_to and path in self.files:
                    self.files[absolute(move_to.group(1).strip(), cwd)] = self.files.pop(path)

    def _apply_hunks(self, path: str, body: str) -> None:
        if path not in self.files:
            self.lose(path)
            return
        lines, search_from = self.files[path].split("\n"), 0
        for hunk in re.split(r"^@@.*$", body, flags=re.M):
            hunk_lines = [line for line in hunk.split("\n") if line[:1] in (" ", "-", "+")]
            if not hunk_lines:
                continue
            old_block = [line[1:] for line in hunk_lines if line[0] in " -"]
            new_block = [line[1:] for line in hunk_lines if line[0] in " +"]
            position = find_block(lines, old_block, search_from)
            if position is None:
                self.lose(path)
                return
            lines[position : position + len(old_block)] = new_block
            search_from = position + len(new_block)
        self.files[path] = "\n".join(lines)


def find_block(lines: list[str], block: list[str], start: int) -> int | None:
    """Index of `block` in `lines`: exact match first, then ignoring surrounding whitespace."""
    for normalize in (lambda text: text, str.strip):
        wanted = [normalize(line) for line in block]
        for index in [*range(start, len(lines)), *range(start)]:
            if [normalize(line) for line in lines[index : index + len(block)]] == wanted:
                return index
    return None


def parse_exit_code(output: str, extra: dict | None, tool: str) -> int | None:
    for pattern in EXIT_CODE_PATTERNS:
        if match := pattern.search(output[:400]):
            return int(match.group(1))
    if match := TRAILING_EXIT_CODE.search(output[-200:]):
        return int(match.group(1))
    if tool in (
        "Bash",
        "bash",
    ):  # Claude Code only flags failures (is_error); no flag means success
        return 1 if (extra or {}).get("is_error") else 0
    return None


class Trajectory:
    def __init__(self, data: dict, path: Path | None = None):
        self.data, self.path = data, path
        self.steps: list[dict] = data["steps"]
        self.session_id = str(data.get("session_id", "trajectory"))
        self.source = data.get("extra", {}).get("source_ref") or str(path or self.session_id)
        self.model = data.get("agent", {}).get("model_name") or "unknown"

    @classmethod
    def load(cls, path: Path) -> Trajectory:
        return cls(json.loads(Path(path).read_text()), Path(path))

    def short_id(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.session_id.lower())[:24].strip("-")

    def user_messages(self) -> list[tuple[int, str]]:
        """(step_id, text) of each real user message, without harness-injected context blocks."""
        messages = []
        for step in self.steps:
            if step["source"] == "user":
                text = " ".join(INJECTED_CONTEXT.sub("", step.get("message") or "").split())
                if text:
                    messages.append((step["step_id"], text))
        return messages

    def date_at(self, step_id: int) -> str:
        """Date (YYYY-MM-DD) of the first timestamped step at or after `step_id`."""
        return next(
            (
                s["timestamp"][:10]
                for s in self.steps
                if s.get("timestamp") and s["step_id"] >= step_id
            ),
            "",
        )

    def replay(self, until_step: int | None = None) -> tuple[list[ShellRun], VirtualFilesystem]:
        """Every shell command (with the files before it) and the final filesystem."""
        filesystem, shell_runs, cwd = (
            VirtualFilesystem(),
            [],
            (self.data.get("extra") or {}).get("cwd"),
        )
        for step in self.steps:
            if until_step is not None and step["step_id"] >= until_step:
                break
            results = {
                r["source_call_id"]: r for r in (step.get("observation") or {}).get("results", [])
            }
            for call in step.get("tool_calls", []):
                tool, args = call["function_name"], call.get("arguments") or {}
                result = results.get(call["tool_call_id"]) or {}
                output = result.get("content") or ""
                if tool in SHELL_TOOLS:
                    command = args.get("command") or args.get("cmd") or ""
                    command = " ".join(command) if isinstance(command, list) else command
                    if args.get("workdir") or leading_cd(command):
                        cwd = args.get("workdir") or absolute(leading_cd(command), cwd)
                    exit_code = (
                        parse_exit_code(output, result.get("extra"), tool) if result else None
                    )
                    shell_runs.append(
                        ShellRun(
                            step["step_id"],
                            command,
                            cwd,
                            exit_code,
                            output,
                            dict(filesystem.files),
                            set(filesystem.lost_files),
                        )
                    )
                    apply_shell_file_changes(filesystem, command, cwd)
                else:
                    apply_file_tool(filesystem, tool, args, output, bool(result), cwd)
        return shell_runs, filesystem


def apply_file_tool(
    fs: VirtualFilesystem, tool: str, args: dict, output: str, has_result: bool, cwd: str | None
) -> None:
    path = args.get("file_path") or args.get("path")
    if tool in WRITE_TOOLS and path and "content" in args:
        fs.write(absolute(path, cwd), args["content"])
    elif (
        tool in EDIT_TOOLS and path and "edits" in args
    ):  # pi: non-overlapping edits, each matched once
        for edit in args["edits"] if isinstance(args["edits"], list) else [None]:
            if (
                isinstance(edit, dict)
                and isinstance(edit.get("oldText"), str)
                and isinstance(edit.get("newText"), str)
            ):
                fs.edit(absolute(path, cwd), edit["oldText"], edit["newText"])
            else:  # a malformed call the harness rejected: we can't tell what the file holds now
                fs.lose(absolute(path, cwd))
    elif tool in EDIT_TOOLS and path:
        old_text = args.get("old_string", args.get("old_str", args.get("oldText")))
        new_text = args.get("new_string", args.get("new_str", args.get("newText")))
        if old_text is None or new_text is None:
            fs.lose(absolute(path, cwd))
        else:
            fs.edit(absolute(path, cwd), old_text, new_text, args.get("replace_all", False))
    elif tool == "MultiEdit" and path:
        for edit in args.get("edits", []):
            fs.edit(
                absolute(path, cwd),
                edit["old_string"],
                edit["new_string"],
                edit.get("replace_all", False),
            )
    elif (
        tool in READ_TOOLS and path and has_result and not (args.get("offset") or args.get("limit"))
    ):
        lines = output.split("\n")
        if lines and LINE_NUMBER_PREFIX.match(lines[0]) and "truncated" not in output:
            fs.seed_if_unseen(
                absolute(path, cwd),
                "\n".join(LINE_NUMBER_PREFIX.sub("", line, count=1) for line in lines),
            )
    elif tool == "apply_patch":
        fs.apply_patch(
            args.get("raw") or args.get("input") or args.get("patch") or "",
            args.get("workdir") or cwd,
        )


def apply_shell_file_changes(fs: VirtualFilesystem, command: str, cwd: str | None) -> None:
    """The file changes a shell command makes that we can rebuild: heredoc writes, inline patches."""
    for match in HEREDOC_WRITE.finditer(command):
        fs.write(absolute(match.group(1), cwd), match.group(3) + "\n")
    if "*** Begin Patch" in command:
        fs.apply_patch(command[command.index("*** Begin Patch") :], cwd)
