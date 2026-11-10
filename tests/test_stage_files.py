"""Large files reach the validation sandbox without exceeding the argv limit."""

from __future__ import annotations

import base64
import re

from repo2rlenv.bootstrap.docker import ExecResult
from repo2rlenv.pipelines._eval_script import stage_files

_LINUX_MAX_ARG_STRLEN = 128 * 1024


class _Recorder:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.staged: dict[str, str] = {}

    def exec(self, command: str, timeout: int = 0) -> ExecResult:
        self.commands.append(command)
        for name in re.findall(r": > (\S+)", command):
            self.staged[name] = ""
        match = re.fullmatch(r"printf %s (\S+) >> (\S+)", command)
        if match:
            self.staged[match.group(2)] += match.group(1)
        return ExecResult(0, "", "", 0.0)


def test_a_large_test_file_is_uploaded_in_chunks():
    big = "def test_x():\n    assert f() == 1\n" * 12_000  # ~400 KB
    sandbox = _Recorder()
    lines = stage_files(sandbox, {"tests/test_big.py": big, "tests/empty.py": ""})
    assert all(len(command.encode()) < _LINUX_MAX_ARG_STRLEN for command in sandbox.commands)
    assert len(sandbox.commands) > 4
    restored = {}
    for line in lines:
        staged, path = re.fullmatch(r"base64 -d < (\S+) > (\S+)", line).groups()
        restored[path] = base64.b64decode(sandbox.staged[staged]).decode()
    assert restored == {"tests/test_big.py": big, "tests/empty.py": ""}


def test_nothing_to_stage():
    sandbox = _Recorder()
    assert stage_files(sandbox, {}) == [] and sandbox.commands == []
