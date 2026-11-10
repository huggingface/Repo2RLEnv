"""Freeze Node test commands at generation, so package.json can't steer grading.

`npm test` runs whatever `scripts.test` says in the package.json it finds, plus
`pretest` and `posttest`, and jest reads its settings from package.json's `jest`
key unless `--config` is given. Validation therefore resolves the command once,
against the base commit, and the frozen command is what validation measures
and what the task grades with:

* `npm test` / `npm run <name>` / `yarn [run] <name>` / `pnpm [run] <name>`
  become the base script bodies (with `pre`/`post` scripts for npm and yarn),
  arguments appended as npm does, run with the repository's own
  `node_modules/.bin` first on PATH;
* jest gets `--ci` (no new snapshots written) and `--config <base config>`;
* mocha gets `--no-config --no-package` when the base has no mocha
  configuration to lose; otherwise its configuration files and package.json
  key are restored from base by the configuration guard;
* vitest gets `--config <base config file>` when there is one.

Every runner configuration source this leaves in play is restored from base by
`_pytest_config_guard` before the tests run (see its key and file lists).
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable

_SCRIPT = re.compile(
    r"^(?P<tool>npm|yarn|pnpm)\s+(?:(?:run|run-script)\s+(?P<name>[\w:.-]+)|(?P<short>test|t))"
    r"(?P<rest>(?:\s.*)?)$"
)
_YARN_SCRIPT = re.compile(r"^(?P<tool>yarn|pnpm)\s+(?P<name>[\w:.-]+)(?P<rest>(?:\s.*)?)$")
_YARN_BUILTINS = {"add", "install", "remove", "upgrade", "exec", "dlx", "why", "info", "list"}
_JEST_CONFIGS = ("jest.config.js", "jest.config.ts", "jest.config.mjs", "jest.config.cjs")
_JEST_CONFIGS += ("jest.config.json",)
_VITEST_CONFIGS = tuple(
    f"{stem}.config.{ext}"
    for stem in ("vitest", "vite")
    for ext in ["ts", "js", "mts", "mjs", "cts", "cjs"]
)
_MOCHA_CONFIGS = tuple(
    f".mocharc{ext}" for ext in ("", ".js", ".cjs", ".json", ".jsonc", ".yml", ".yaml")
)
_PREFIX = r"(?:(?<=^)|(?<=[\s;&|(]))(?:npx\s+|(?:\./)?node_modules/\.bin/)?"


def _scripts(read_base: Callable[[str], str | None]) -> dict[str, str]:
    raw = read_base("package.json")
    try:
        manifest = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    scripts = manifest.get("scripts") if isinstance(manifest, dict) else None
    return {k: v for k, v in (scripts or {}).items() if isinstance(v, str)}


def _resolve(segment: str, scripts: dict[str, str], depth: int = 0) -> str:
    """One `&&` segment with npm/yarn/pnpm script calls replaced by their bodies."""
    text = segment.strip()
    match = _SCRIPT.match(text)
    if match is None:
        match = _YARN_SCRIPT.match(text)
        if match is None or match.group("name") in _YARN_BUILTINS:
            return segment
        name = match.group("name")
    else:
        name = match.group("name") or "test"
    body = scripts.get(name)
    if body is None or depth >= 3:
        return segment
    rest = match.group("rest").strip()
    if rest.startswith("-- "):
        rest = rest[3:]
    elif rest == "--":
        rest = ""
    tool = match.group("tool")
    parts = []
    if tool in ("npm", "yarn") and f"pre{name}" in scripts:
        parts.append(resolve_command(scripts[f"pre{name}"], scripts, depth + 1))
    parts.append(resolve_command(f"{body} {rest}".strip(), scripts, depth + 1))
    if tool in ("npm", "yarn") and f"post{name}" in scripts:
        parts.append(resolve_command(scripts[f"post{name}"], scripts, depth + 1))
    return " && ".join(f"( {part} )" for part in parts)


def resolve_command(command: str, scripts: dict[str, str], depth: int = 0) -> str:
    return " && ".join(_resolve(segment, scripts, depth) for segment in command.split("&&"))


def _add_flags(command: str, runner: str, flags: str) -> str:
    """Insert `flags` right after each call of `runner` that lacks them."""
    pattern = re.compile(_PREFIX + re.escape(runner) + r"(?=\s|$|\))")

    def insert(match: re.Match) -> str:
        return match.group(0) + " " + flags

    return pattern.sub(insert, command)


def freeze_node_commands(test_cmds: list[str], read_base: Callable[[str], str | None]) -> list[str]:
    """`test_cmds` with package.json indirection resolved against base (see module doc)."""
    joined = " && ".join(test_cmds)
    if not re.search(r"\b(npm|yarn|pnpm|jest|mocha|vitest)\b", joined):
        return test_cmds
    scripts = _scripts(read_base)
    frozen = [resolve_command(cmd, scripts) for cmd in test_cmds]
    # Config paths are relative to the repository root; a command that changes
    # directory keeps its runner flags as they are.
    relocates = any(re.search(r"(^|[\s;&|(])cd\s", cmd) for cmd in frozen)
    out = []
    for original, cmd in zip(test_cmds, frozen, strict=True):
        if cmd != original or re.search(r"\b(jest|mocha|vitest)\b", cmd):
            cmd = 'export PATH="$PWD/node_modules/.bin:$PATH" && ' + cmd
        if re.search(r"\bjest\b", cmd) and "--ci" not in cmd:
            cmd = _add_flags(cmd, "jest", "--ci")
        if (
            not relocates
            and re.search(r"\bjest\b", cmd)
            and not re.search(r"(\s--config|\s-c\s)", cmd)
        ):
            config = next((name for name in _JEST_CONFIGS if read_base(name) is not None), None)
            if config is None:
                raw = read_base("package.json")
                try:
                    if raw and "jest" in json.loads(raw):
                        config = "package.json"
                except (ValueError, TypeError):
                    pass
            if config is not None:
                cmd = _add_flags(cmd, "jest", f"--config {shlex.quote(config)}")
        if re.search(r"\bmocha\b", cmd) and "--no-config" not in cmd:
            raw = read_base("package.json") or ""
            try:
                has_key = "mocha" in json.loads(raw) if raw else False
            except (ValueError, TypeError):
                has_key = True
            if not has_key and not any(read_base(name) is not None for name in _MOCHA_CONFIGS):
                cmd = _add_flags(cmd, "mocha", "--no-config --no-package")
        if not relocates and re.search(r"\bvitest\b", cmd) and "--config" not in cmd:
            config = next((name for name in _VITEST_CONFIGS if read_base(name) is not None), None)
            if config is not None:
                cmd = _add_flags(cmd, "vitest", f"--config {shlex.quote(config)}")
        out.append(cmd)
    return out
