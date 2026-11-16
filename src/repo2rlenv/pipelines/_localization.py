"""Localization mission: a report-only sibling of a validated remediation task.

Where a remediation task asks the agent to *fix* a defect and grades the patch, a
localization task asks it to *find* the defect and write a report. Both are built
from the same validated candidate and share the same scrubbed image, 
so the pair measures two different skills on one defect.

The localization task is derived from the remediation ``HarborTask`` rather than
rebuilt from scratch: it reuses the Dockerfile (history scrubbed to the base
commit, pristine copy stashed) and the egress guard unchanged, so the
anti-contamination guarantees of the remediation task hold here too. The
environment enforces them; the instruction never asks.

Grading (RFC 0035, phase 2). The fix's changed lines on the OLD side become
``tests/oracle.json``: the files, and the line regions inside them, that a correct
report has to point at. The verifier (``_pr_localization_verifier``) finds where each
quotation of the report lives in the base tree and scores file F1 and region overlap
against that oracle. An optional judge can lower the score, never raise it. The oracle
reaches the container only at verification time (``tests/``); the reference solver gets
its own copy under ``solution/``, which the agent's container never sees.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path, PurePosixPath

from repo2rlenv.emitter.harbor import HarborTask, write_harbor_task
from repo2rlenv.pipelines._env_guard import egress_guard_compose, fix_source_hosts
from repo2rlenv.pipelines._pr_localization_verifier import (
    FILE_WEIGHT,
    MAX_ORACLE_FILES,
    MAX_ORACLE_HUNKS,
    REGION_SLACK_LINES,
    REGION_WEIGHT,
    REQUIRED_SECTIONS,
)
from repo2rlenv.pipelines.pr_runtime import (
    MIN_HARBOR_VERSION,
    SEPARATE_VERIFIER,
    ensure_system_python,
    separate_head,
    separate_verifier_files,
    stash_pristine,
)
from repo2rlenv.sources import SourceKind

LOCALIZATION_SUFFIX = "-loc"
REPORT_PATH = "/workspace/FINDINGS.md"
REPORT_MAX_BYTES = 65536
LOCALIZATION_TIMEOUT_SEC = 300.0  # no build, no test run: read one report, search one tree
SKIP_NO_HUNKS = "no_localizable_hunks"
SKIP_TOO_BROAD = "localization_too_broad"
# Two changed regions closer than this are one region: a single quotation covers both.
MERGE_GAP_LINES = 2 * REGION_SLACK_LINES

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_OLD_FILE_RE = re.compile(r"^--- (?:a/(\S+)|/dev/null)")
# The remediation instruction ends with a "## Task" section asking for a patch.
_TASK_SECTION_RE = re.compile(r"\n+## Task\b")

# A fix that touches only these has no code to localize.
_PROSE_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc", ".markdown"})
_PROSE_STEMS = frozenset(
    {"changes", "changelog", "history", "news", "license", "licence", "authors", "contributors"}
)
_PROSE_DIRS = frozenset({"docs", "doc", ".github", "changelog.d", "news"})
_LOCKFILES = frozenset(
    {
        "poetry.lock",
        "uv.lock",
        "pipfile.lock",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "cargo.lock",
        "go.sum",
        "gemfile.lock",
        "composer.lock",
    }
)

INSTRUCTION_TASK = """## Task

Investigate the defect behind the issue above. **Do not modify the repository** and do not
propose a patch: only a report is graded, and nothing else you change is kept.

Write `{report}` as Markdown with these sections, each heading on its own line and in this
order:

## operation
How the symptom is triggered: the entry point (command, route, function or API call) and the
input that reaches the faulty behaviour.

## paths
The complete relative source paths (from the repository root, not basenames alone) of every
file where the faulty behaviour is implemented, with the functions concerned. Quote the faulty
code itself in fenced code blocks, **copied verbatim** from the repository (checked out at
base commit `{base}`): not retyped, not a search-output dump, and not code from elsewhere that
merely looks related.

## chain
The sequence of calls from the entry point to the faulty code, in order.

## cause
Why that code produces the symptom described above, in your own words.

## impact
One concrete, observable effect of the defect.

You may add an `## unaffected` section after these, saying what keeps working. Each section is
read as a claim: a report that only names keywords, only quotes code, or only describes the
symptom is not accepted, and neither is one that leaves a section out or reorders them. Naming
or quoting files that are not part of the defect lowers the score."""

# Runs in the agent's container as the reference solution. It writes a report that quotes
# the changed regions, checking each quotation against the tree with the verifier's own
# search (shipped next to it as locate.py) so that the reference can never quote code
# that points nowhere in particular.
SOLVE_SH = """#!/bin/bash
# Reference solution: a report quoting the code the fix changed.
set -euo pipefail
cd /workspace
HERE="$(dirname "$0")"
exec python3 "$HERE/solve.py" "$HERE/oracle.json" /workspace/FINDINGS.md /workspace
"""

SOLVE_PY = r'''"""Reference localizer: write a report that quotes each region the fix changed."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import locate  # noqa: E402  (a copy of the task's verifier)

CHUNK_CHARS = 560  # whitespace-collapsed; the verifier accepts up to 600
RAW_CHARS = 650
LOOKAHEAD_LINES = 60
SEARCH_BEFORE = 8


def window(lines, first):
    """Greedy chunk of lines from index ``first`` that fits the quotation limits."""
    chunk = []
    for line in lines[first : first + LOOKAHEAD_LINES]:
        trial = chunk + [line]
        text = "\n".join(trial)
        if len(locate.squash(text)) > CHUNK_CHARS or len(text) > RAW_CHARS:
            break
        chunk = trial
    if not chunk and first < len(lines):
        chunk = [lines[first][:RAW_CHARS]]
    return chunk


def covers(places, hunk):
    return any(
        place.path == hunk["path"]
        and max(place.start, hunk["start"]) <= min(place.end, hunk["end"]) + locate.REGION_SLACK_LINES
        for place in places
    )


def pick(tree, lines, hunk):
    """A quotation that lies in ``hunk`` and does not occur in too many files."""
    lo = max(hunk["start"] - 1, 0)
    hi = min(hunk["end"], len(lines))
    starts = list(range(lo, hi)) + list(range(lo - 1, max(lo - SEARCH_BEFORE, 0) - 1, -1))
    for first in starts:
        chunk = window(lines, first)
        text = locate.squash("\n".join(chunk))
        if len(text) < locate.MIN_EXCERPT_CHARS:
            continue
        places = tree.locate(text)
        if not places or len({place.path for place in places}) > locate.MAX_FILES_PER_EXCERPT:
            continue
        if covers(places, hunk):
            return chunk
    return None


def main(oracle_path, out_path, root):
    with open(oracle_path, encoding="utf-8") as handle:
        oracle = json.load(handle)
    tree = locate.Tree(root)
    quotes = []
    missed = []
    for hunk in oracle["hunks"]:
        with open(os.path.join(root, hunk["path"]), encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
        chunk = pick(tree, lines, hunk)
        if chunk is None:
            missed.append(hunk)
        else:
            quotes.append((hunk, chunk))
    files = list(dict.fromkeys(hunk["path"] for hunk, _ in quotes))
    out = [
        "# Findings",
        "",
        "## operation",
        "",
        "The symptom described in the issue is triggered through the entry point it names, "
        "with the input it describes; that input reaches the code quoted under paths.",
        "",
        "## paths",
        "",
        "The behaviour is implemented in: " + ", ".join("`" + name + "`" for name in files) + ".",
        "",
    ]
    for hunk, chunk in quotes:
        out += ["From `" + hunk["path"] + "`, around line " + str(hunk["start"]) + ":", "", "```", *chunk, "```", ""]
    out += [
        "## chain",
        "",
        "The call reaches these files in the order listed under paths, from the entry point "
        "down to the quoted code, where the faulty behaviour happens.",
        "",
        "## cause",
        "",
        "The quoted code does not handle the case the issue describes correctly, so the "
        "symptom follows directly from it each time that path runs.",
        "",
        "## impact",
        "",
        "The behaviour the issue reports is observable whenever that path runs.",
        "",
    ]
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(out))
    if missed:
        sys.stderr.write(
            "reference solver: no unique quotation for %d changed region(s): %s\n"
            % (len(missed), ", ".join("%s:%d-%d" % (h["path"], h["start"], h["end"]) for h in missed))
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
'''


def is_localizable(path: str) -> bool:
    """False for files a defect is not located in: docs, changelogs, lockfiles, CI config."""
    pure = PurePosixPath(path)
    name = pure.name.lower()
    if name in _LOCKFILES or pure.suffix.lower() in _PROSE_SUFFIXES:
        return False
    if pure.stem.lower() in _PROSE_STEMS:
        return False
    return not any(part.lower() in _PROSE_DIRS for part in pure.parts[:-1])


def old_hunk_ranges(diff: str) -> dict[str, list[tuple[int, int]]]:
    """For each file the diff changes, the ``(start, end)`` line ranges of its hunks, OLD side.

    The agent reads the tree at the base commit, so the lines that matter are the
    ``-X,Y`` numbers of ``@@ -X,Y +A,B @@`` (``_pr_diff_verifier.hunk_ranges`` reports the
    new side). These include each hunk's context lines; ``changed_regions`` is the
    tighter, graded version. Files the diff creates have no old side and are left out,
    as are files without a textual hunk. A pure insertion (``-X,0``) is reported as the
    single line it follows.
    """
    out: dict[str, list[tuple[int, int]]] = {}
    path: str | None = None
    for line in diff.splitlines():
        header = _DIFF_HEADER_RE.match(line)
        if header:
            path = header.group(1)
            continue
        old = _OLD_FILE_RE.match(line)
        if old:
            path = old.group(1)  # None for /dev/null: a created file
            continue
        hunk = _HUNK_HEADER_RE.match(line)
        if hunk and path is not None:
            start = int(hunk.group(1))
            count = 1 if hunk.group(2) is None else int(hunk.group(2))
            first = max(start, 1)
            out.setdefault(path, []).append((first, max(first, start + count - 1)))
    return out


def _merge(lines: set[int]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for line in sorted(lines):
        if ranges and line - ranges[-1][1] <= MERGE_GAP_LINES:
            ranges[-1] = (ranges[-1][0], line)
        else:
            ranges.append((line, line))
    return ranges


def changed_regions(diff: str) -> dict[str, list[tuple[int, int]]]:
    """For each file, the OLD-side regions the diff actually changes (no context lines).

    A removed or replaced line is its own line number. An insertion is the line it follows.
    Regions closer than ``MERGE_GAP_LINES`` merge, so one quotation can cover them. Hunks
    are walked by their header counts, because a removed line can itself start with
    ``-- `` and look like a file header.
    """
    touched: dict[str, set[int]] = {}
    lines = diff.splitlines()
    path: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        header = _DIFF_HEADER_RE.match(line)
        if header:
            path = header.group(1)
            i += 1
            continue
        old_file = _OLD_FILE_RE.match(line)
        if old_file:
            path = old_file.group(1)  # None for /dev/null: a created file
            i += 1
            continue
        hunk = _HUNK_HEADER_RE.match(line)
        i += 1
        if not hunk:
            continue
        old_line = int(hunk.group(1))
        old_left = 1 if hunk.group(2) is None else int(hunk.group(2))
        new_left = 1 if hunk.group(4) is None else int(hunk.group(4))
        if old_left == 0:
            old_line += 1  # `-X,0`: the lines go in after line X
        while i < len(lines) and (old_left > 0 or new_left > 0):
            body = lines[i]
            if body.startswith(("@@", "diff --git")):
                break  # a header can never be a body line: the counts were wrong
            i += 1
            tag = body[:1]
            if tag == "\\":
                continue
            if tag == "-":
                if path is not None:
                    touched.setdefault(path, set()).add(old_line)
                old_line += 1
                old_left -= 1
            elif tag == "+":
                if path is not None:
                    touched.setdefault(path, set()).add(max(old_line - 1, 1))
                new_left -= 1
            else:
                old_line += 1
                old_left -= 1
                new_left -= 1
    return {name: _merge(found) for name, found in touched.items()}


def build_oracle(patch: str) -> tuple[dict[str, object] | None, str | None]:
    """``(oracle, None)`` for ``tests/oracle.json``, or ``(None, skip reason)``."""
    regions = {
        path: spans for path, spans in changed_regions(patch).items() if is_localizable(path)
    }
    if not regions:
        return None, SKIP_NO_HUNKS
    hunks = [
        {"path": path, "start": start, "end": end}
        for path, spans in regions.items()
        for start, end in spans
    ]
    if len(regions) > MAX_ORACLE_FILES or len(hunks) > MAX_ORACLE_HUNKS:
        return None, SKIP_TOO_BROAD
    return {"files": list(regions), "hunks": hunks}, None


def redact_paths(text: str, paths: list[str]) -> str:
    """Replace the fix's file paths (and bare file names) in ``text`` with ``[file]``.

    An issue sometimes names the file it is about. For a remediation task that is
    harmless; for localization it hands over the answer. This is a heuristic on
    names that look like files: a defect described by module or function name only
    is not touched.
    """
    names: set[str] = set(paths)
    for path in paths:
        base = path.rsplit("/", 1)[-1]
        if "." in base.strip(".") and len(base) >= 4:
            names.add(base)
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", "[file]", text)
    return text


def symptom_of(instruction: str) -> str:
    """The issue text of a remediation instruction, without its ``## Task`` section."""
    return _TASK_SECTION_RE.split(instruction, maxsplit=1)[0].strip()


def build_instruction(remediation_instruction: str, paths: list[str], base_commit: str) -> str:
    symptom = redact_paths(symptom_of(remediation_instruction), paths)
    task = INSTRUCTION_TASK.format(report=REPORT_PATH, base=base_commit[:12])
    return f"{symptom}\n\n{task}"


def build_test_script() -> str:
    """tests/test.sh of a localization task (same contract as the other separate verifiers)."""
    return (
        separate_head(None)
        + f"""
R2E_REPORT={REPORT_PATH}
[ -f "$R2E_REPORT" ] || r2e_finish graded missing_report 'no report submitted'
R2E_CHECK="$("$R2E_PY" -B -I -S "$SCRIPT_DIR/grader_tools.py" check --path "$R2E_REPORT" --max-bytes {REPORT_MAX_BYTES})"
[ "$?" -eq 0 ] && [ "$R2E_CHECK" = ok ] || r2e_finish graded invalid_report 'report is not a small regular file'
"$R2E_PY" -B -I -S "$SCRIPT_DIR/verifier.py" --report "$R2E_REPORT" --pristine /opt/r2e/pristine --oracle "$SCRIPT_DIR/oracle.json" --instruction "$SCRIPT_DIR/instruction.md" --max-bytes {REPORT_MAX_BYTES}
R2E_VERIFIER_RC=$?
[ "$R2E_VERIFIER_RC" -eq 3 ] && r2e_finish ungraded verifier_error 'grader inputs are unusable'
[ "$R2E_VERIFIER_RC" -eq 0 ] || r2e_finish graded grader_crashed 'the report could not be graded'
r2e_open_logs
"""
    )


# Harbor's native agent (terminus-2) drives the shell through tmux. The agent sandbox is offline and
# non-root, so it cannot install it at run time. apt only: the bootstrap images the runtime pipelines
# use are Debian-based, and an image without apt is left as it is rather than broken.
TMUX_LINES = (
    "# tmux: terminus-2 drives the shell through it, and an offline agent cannot install it.\n"
    "RUN (command -v tmux >/dev/null 2>&1) || \\\n"
    "    (apt-get update && apt-get install -y --no-install-recommends tmux \\\n"
    "     && rm -rf /var/lib/apt/lists/*) || \\\n"
    "    apk add --no-cache tmux || true\n"
)

# The generic attack scripts, shipped under tests/shortcuts/ and not baked into any image: a harness
# mounts tests/ and runs them against the task's own test.sh (see _localization_controls).
CONTROL_NAMES = (
    ("01_noop", "noop"),
    ("02_keywords", "keywords"),
    ("03_wrong_location", "wrong_location"),
    ("04_paths_only", "paths_only"),
    ("05_wrong_order", "wrong_order"),
    ("06_missing_section", "missing_section"),
    ("99_infra_down", "infra_down"),
)


def _controls_files() -> dict[str, str]:
    """``tests/shortcuts/``: the controls, one thin wrapper per attack, and a runner."""
    files = {
        "tests/shortcuts/controls.py": (
            Path(__file__).parent / "_localization_controls.py"
        ).read_text(encoding="utf-8")
    }
    for stem, control in CONTROL_NAMES:
        files[f"tests/shortcuts/{stem}.sh"] = (
            "#!/bin/bash\n"
            f"# Control: {control}. Exit 0 = the verifier refused it as it must, 1 = it did not.\n"
            f'exec python3 -B -I -S "$(dirname "$0")/controls.py" {control}\n'
        )
    files["tests/shortcuts/run_all.sh"] = (
        "#!/bin/bash\n"
        "# Run every control against this task's test.sh. Needs the task image with tests/ mounted\n"
        "# at /tests and the report path writable. Exit 0 only if every control behaved.\n"
        "set -u\n"
        "rc=0\n"
        'for script in "$(dirname "$0")"/[0-9]*.sh; do\n'
        '  bash "$script" || rc=1\n'
        "done\n"
        "exit $rc\n"
    )
    return files


def _grading_tree_dockerfile(dockerfile: str) -> str:
    """Append the pristine copy a report is checked against to a task's Dockerfile.

    The runtime pipelines' images already end this way. ``pr_diff``'s does not (its
    verifier runs in the agent's own container), so its sibling gets the same tail:
    a root-only copy of the scrubbed repository that the fresh verifier container
    reads, out of reach of anything the agent does to ``/workspace``.
    """
    return dockerfile.rstrip("\n") + "\n" + ensure_system_python() + stash_pristine()


def _verifier_source() -> str:
    return (Path(__file__).parent / "_pr_localization_verifier.py").read_text(encoding="utf-8")


def build_localization_task(
    base: HarborTask,
    *,
    patch: str,
    base_commit: str,
    stash_tree: bool = False,
    source_kind: SourceKind | None = None,
    offline: bool = True,
) -> tuple[HarborTask | None, str | None]:
    """Derive the localization sibling of ``base``.

    ``patch`` is the gold SOURCE patch (not the test patch). Returns ``(task, None)``
    or ``(None, reason)`` when there is nothing to localize: the fix only creates files
    or only touches docs, changelogs and lockfiles (``no_localizable_hunks``), or it
    spreads over more files or regions than a report can reasonably cover
    (``localization_too_broad``).

    ``stash_tree`` is for a base task that has no separate verifier of its own
    (``pr_diff``): the sibling gets the pristine copy, the trusted helpers and the
    egress guard that the runtime pipelines' tasks already carry. The base task's
    own Dockerfile and files are left as they were.

    ``offline`` sets ``[agent] network_mode = "no-network"``. Harbor enforces that per phase with a
    firewall that needs ``nft_fib`` in the Docker host's kernel, and refuses the task where it is
    missing (Docker Desktop on macOS, Modal). Without it the egress guard's hostname denylist is
    the only network control, as for the remediation task.
    """
    oracle, reason = build_oracle(patch)
    if oracle is None:
        return None, reason
    if base.environment_dockerfile is None:
        raise ValueError(f"{base.name}: localization needs the remediation task's environment")

    dockerfile = base.environment_dockerfile
    if stash_tree:
        dockerfile = _grading_tree_dockerfile(dockerfile)
    dockerfile = dockerfile.rstrip("\n") + "\n" + TMUX_LINES
    # Everything the fix touches is redacted from the issue, not only what is graded.
    touched = list(old_hunk_ranges(patch))
    r2e = {
        key: value
        for key, value in base.repo2env.items()
        # Per-remediation facts (F2P/P2P, reward calibration) and the pipeline's own
        # subtable describe the patch task, not this one.
        if key not in {"reward_kinds", "reward_calibration", base.repo2env.get("pipeline")}
    }
    r2e["reward_kinds"] = ["localization"]
    r2e["mission"] = "localization"
    r2e["localization"] = {
        "source_task": base.name,
        "report_path": REPORT_PATH,
        "grading": "oracle",
        "gold_files": len(oracle["files"]),  # type: ignore[arg-type]
        "gold_regions": len(oracle["hunks"]),  # type: ignore[arg-type]
        "file_weight": FILE_WEIGHT,
        "region_weight": REGION_WEIGHT,
        # Opt-in at run time: `--ve R2E_LOC_JUDGE=1 --ve ANTHROPIC_API_KEY=...`.
        "judge": "optional",
        "agent_network": "offline" if offline else "egress_guard_only",
        "sections": list(REQUIRED_SECTIONS),
        "controls": [stem for stem, _ in CONTROL_NAMES],
        "harbor_min_version": MIN_HARBOR_VERSION,
    }
    # The egress guard and the history scrub travel with the remediation task's files.
    aux = {
        path: content
        for path, content in base.aux_files.items()
        if path.startswith("environment/") or path == "tests/grader_tools.py"
    }
    if stash_tree:
        # `launcher.py` rides along with the helpers; localization runs no tests and never
        # calls it.
        aux.update(separate_verifier_files())
        aux["environment/docker-compose.yaml"] = egress_guard_compose(fix_source_hosts(source_kind))
    instruction = build_instruction(base.instruction, touched, base_commit)
    oracle_json = json.dumps(oracle, indent=2) + "\n"
    verifier = _verifier_source()
    aux["tests/verifier.py"] = verifier
    aux["tests/oracle.json"] = oracle_json
    aux["tests/instruction.md"] = instruction  # the judge's copy; tests/ is the only dir mounted
    aux.update(_controls_files())
    aux["solution/oracle.json"] = oracle_json
    aux["solution/locate.py"] = verifier
    aux["solution/solve.py"] = SOLVE_PY

    return (
        replace(
            base,
            name=f"{base.name}{LOCALIZATION_SUFFIX}",
            instruction=instruction,
            oracle_diff=patch,
            environment_dockerfile=dockerfile,
            repo2env=r2e,
            category="localization",
            description=f"Locate the defect: {base.description}",
            keywords=[*base.keywords, "localization"],
            agent={"network_mode": "public"} if offline else {},
            test_script=build_test_script(),
            verifier={**SEPARATE_VERIFIER, "timeout_sec": LOCALIZATION_TIMEOUT_SEC},
            artifacts=[{"source": REPORT_PATH}],
            aux_files=aux,
            solve_script=SOLVE_SH,
        ),
        None,
    )


def emit_missions(
    task: HarborTask,
    out_dir: Path,
    *,
    mission: str,
    patch: str,
    base_commit: str,
    stash_tree: bool = False,
    source_kind: SourceKind | None = None,
    offline: bool = True,
) -> tuple[list[str], str | None]:
    """Write the task(s) ``mission`` asks for. Returns ``(task names written, skip reason)``.

    The reason is set only when nothing was written: ``localization`` was asked for
    and there is nothing to localize. With ``both`` the remediation task is still
    written in that case.
    """
    written: list[str] = []
    if mission in ("remediation", "both"):
        write_harbor_task(task, out_dir)
        written.append(task.name)
    if mission in ("localization", "both"):
        localization, reason = build_localization_task(
            task,
            patch=patch,
            base_commit=base_commit,
            stash_tree=stash_tree,
            source_kind=source_kind,
            offline=offline,
        )
        if localization is not None:
            write_harbor_task(localization, out_dir)
            written.append(localization.name)
        elif not written:
            return [], reason
    return written, None
