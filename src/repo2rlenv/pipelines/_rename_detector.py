"""Rename-refactor detector for `refactor_synthesis`.

Two layers, both pure stdlib:

  1. **Commit-message regex** — match phrases like "rename X to Y" /
     "renamed method foo to bar" / "rename `OldClass` to `NewClass`"
  2. **Diff verification** — confirm the commit's diff actually performs
     the rename (old token removed, new token added, no surviving
     `def OLD(...) / class OLD ...` in the after-state)

Both filters together give a low false-positive rate. False negatives
(unannounced renames) are accepted as a v0.8 trade-off; we leave AST
diff matching to a future release.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class RenameMatch:
    """One detected rename refactor (commit message regex + diff verified)."""

    old_name: str
    new_name: str
    kind: str  # "function" / "method" / "class" / "argument" / "symbol" / ""


# ---------------------------------------------------------------------------
# Commit-message regex
# ---------------------------------------------------------------------------

# Capture the "kind" word optionally (function/method/class/arg/variable/etc.)
# Old/new names can be wrapped in backticks; we strip them out.
_RENAME_RE = re.compile(
    r"""
    \b rename (?:s|d)? \s+                                      # rename / renamed / renames
    (?: (?:the\s+)?
        (?P<kind> function | method | class | variable | symbol
                | parameter | param | arg(?:ument)? | field | attribute | module )
        \s+
    )?
    `? (?P<old> [A-Za-z_][A-Za-z0-9_]* ) `?
    \s+ to \s+
    `? (?P<new> [A-Za-z_][A-Za-z0-9_]* ) `?
    """,
    re.IGNORECASE | re.VERBOSE,
)


_KIND_ALIASES = {
    "arg": "argument",
    "param": "parameter",
}


def find_rename_in_message(message: str) -> tuple[str, str, str] | None:
    """Parse a commit message; return (old, new, kind) or None.

    The kind is normalized (`arg` → `argument`, `param` → `parameter`).
    If the pattern doesn't match, returns None. If old == new, returns
    None (defensive).
    """
    m = _RENAME_RE.search(message)
    if not m:
        return None
    old = m.group("old")
    new = m.group("new")
    if old == new:
        return None
    kind_raw = (m.group("kind") or "").lower()
    kind = _KIND_ALIASES.get(kind_raw, kind_raw)
    return old, new, kind


# ---------------------------------------------------------------------------
# Diff verification
# ---------------------------------------------------------------------------


def _bare_word(token: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(token)}\b")


# Patterns that mean "the old name still has a definition in the file":
# `def OLD(`, `def OLD :`, `async def OLD(`, `class OLD(`, `class OLD :`, `class OLD\n`
def _redefines_pattern(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"^(?:\s*)(?:async\s+def|def|class)\s+{re.escape(name)}\s*[:(\s]",
        re.MULTILINE,
    )


@dataclass(slots=True, frozen=True)
class DiffVerifyOutcome:
    ok: bool
    reason: str  # "" when ok; otherwise a short skip reason


def verify_rename_in_diff(unified_diff: str, *, old_name: str, new_name: str) -> DiffVerifyOutcome:
    """Confirm the diff actually performs `old_name → new_name`.

    Required:
      (a) At least one `-`-line **removes a def/class definition** of `old_name`
          (proves this is a real symbol rename, not just a parameter or string change)
      (b) At least one `+`-line **adds a def/class definition** of `new_name`

    We deliberately ALLOW `+def old_name(...)` to remain — many public Python
    libraries keep a back-compat shim with the old name forwarding to the
    new implementation. Rejecting those would filter out most real-world
    rename refactors in mature codebases.
    """
    if not unified_diff or not unified_diff.strip():
        return DiffVerifyOutcome(False, "empty_diff")

    old_def_pat = _redefines_pattern(old_name)
    new_def_pat = _redefines_pattern(new_name)

    removed_old_def = False
    added_new_def = False
    for raw_line in unified_diff.splitlines():
        if raw_line.startswith("---") or raw_line.startswith("+++"):
            continue  # diff metadata
        if raw_line.startswith("-") and old_def_pat.match(raw_line[1:]):
            removed_old_def = True
        elif raw_line.startswith("+") and new_def_pat.match(raw_line[1:]):
            added_new_def = True

    if not removed_old_def:
        return DiffVerifyOutcome(False, "old_def_not_removed")
    if not added_new_def:
        return DiffVerifyOutcome(False, "new_def_not_added")
    return DiffVerifyOutcome(True, "")
