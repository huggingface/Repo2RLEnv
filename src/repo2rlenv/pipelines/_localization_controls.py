"""Attack scripts for a localization task (stdlib only; runs as ``python3 -B -I -S``).

Ships as ``tests/shortcuts/controls.py`` next to thin ``NN_name.sh`` wrappers. Each control
writes a report a lazy or hostile agent might write, runs the task's real ``tests/test.sh``
on it, and checks the outcome against what the task must do. A task whose verifier lets one
through does not measure what it claims to.

Run them in the task's own image, with the task's ``tests/`` mounted::

    docker build -t loc-img <task>/environment
    docker run --rm -v <task>/tests:/tests loc-img bash /tests/shortcuts/run_all.sh

Every control builds its material from the task itself (``/tests/oracle.json`` and
``/opt/r2e/pristine``), so the same scripts work for any task. A control checks the exact
``parse_status`` it expects, not only that the reward is 0: a report that is refused for the
wrong reason would hide a gate that does not work.

Exit status of a control: 0 = the verifier behaved correctly, 1 = it did not, 2 = the control
could not be built for this task (reported, never counted as a pass).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

TESTS = "/tests"
PRISTINE = "/opt/r2e/pristine"
REPORT = "/workspace/FINDINGS.md"
LOGS = "/logs/verifier"

sys.path.insert(0, TESTS)
import verifier as v  # noqa: E402  (the task's own grader, from /tests)

PATHS_TEXT = (
    "The faulty behaviour is implemented in the code quoted below, which the report names and "
    "quotes from the repository as it is checked out."
)
PROSE = (
    "This section explains the behaviour in the report's own words so that it is long enough "
    "to count as written reasoning and not as a list of keywords."
)


class CannotBuild(Exception):
    """This task has no material for the control (e.g. no second file to quote)."""


def load_oracle() -> dict:
    with open(os.path.join(TESTS, "oracle.json"), encoding="utf-8") as handle:
        return json.load(handle)


def read_lines(relative: str) -> list[str]:
    with open(os.path.join(PRISTINE, relative), encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def quote_from(tree: v.Tree, relative: str, start: int, stop: int) -> str:
    """A verbatim chunk of ``relative`` between lines ``start`` and ``stop`` that counts.

    "Counts" is the grader's own rule: long enough, short enough, and not found in so many
    files that it points nowhere.
    """
    lines = read_lines(relative)
    for first in range(max(start - 1, 0), min(stop, len(lines))):
        chunk: list[str] = []
        for line in lines[first : first + 40]:
            trial = [*chunk, line]
            if len(v.squash("\n".join(trial))) > 560:
                break
            chunk = trial
        text = "\n".join(chunk)
        collapsed = v.squash(text)
        if not v.MIN_EXCERPT_CHARS <= len(collapsed) <= v.MAX_EXCERPT_CHARS:
            continue
        places = tree.locate(collapsed)
        if places and len({p.path for p in places}) <= v.MAX_FILES_PER_EXCERPT:
            return text
    raise CannotBuild(f"no usable quotation in {relative}:{start}-{stop}")


def gold_quote(tree: v.Tree, oracle: dict) -> str:
    """A quotation of the code the fix changes."""
    for hunk in oracle["hunks"]:
        try:
            return quote_from(tree, hunk["path"], hunk["start"], hunk["end"])
        except CannotBuild:
            continue
    raise CannotBuild("no changed region of the oracle can be quoted uniquely")


def unrelated_quote(tree: v.Tree, oracle: dict) -> str:
    """A real quotation from a file the fix does not touch."""
    gold = set(oracle["files"])
    for relative in sorted(tree.files):
        if relative in gold or not relative.endswith((".py", ".js", ".ts", ".go", ".rs", ".java")):
            continue
        try:
            return quote_from(tree, relative, 1, len(read_lines(relative)))
        except CannotBuild:
            continue
    raise CannotBuild("no file outside the oracle has a usable quotation")


def report(quote: str, *, paths_text: str, order: tuple[str, ...] | None = None) -> str:
    """A well-formed report: every required section, prose in each, one quotation."""
    body = {
        "operation": PROSE,
        "paths": f"{paths_text}\n\n```\n{quote}\n```" if quote else paths_text,
        "chain": PROSE,
        "cause": PROSE,
        "impact": PROSE,
    }
    names = order or v.REQUIRED_SECTIONS
    return "# Findings\n\n" + "\n\n".join(f"## {name}\n\n{body[name]}" for name in names) + "\n"


def write_report(text: str | None) -> None:
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    if text is None:
        if os.path.lexists(REPORT):
            os.unlink(REPORT)
        return
    with open(REPORT, "w", encoding="utf-8") as handle:
        handle.write(text)


def run_test_sh() -> dict:
    """Run the task's real test.sh and return what it left behind."""
    for stale in ("reward.txt", "reward.json", "reward-details.json"):
        try:
            os.unlink(os.path.join(LOGS, stale))
        except FileNotFoundError:
            pass
    subprocess.run(["bash", os.path.join(TESTS, "test.sh")], capture_output=True, check=False)
    reward_path = os.path.join(LOGS, "reward.txt")
    details_path = os.path.join(LOGS, "reward-details.json")
    reward = None
    if os.path.exists(reward_path):
        with open(reward_path, encoding="utf-8") as handle:
            reward = float(handle.read().strip())
    details = {}
    if os.path.exists(details_path):
        with open(details_path, encoding="utf-8") as handle:
            details = json.load(handle)
    return {"reward": reward, "details": details}


def expect(name: str, outcome: dict, *, status: str, graded: bool) -> int:
    """Check the verdict: a graded 0 with this status, or an ungraded run with no reward."""
    details = outcome["details"]
    got = details.get("parse_status")
    if graded:
        ok = (
            outcome["reward"] == 0.0
            and details.get("evaluation_status") == "graded"
            and got == status
        )
    else:
        ok = outcome["reward"] is None and details.get("evaluation_status") == "ungraded"
    verdict = "ok" if ok else "MISMATCH"
    print(
        f"  {name:<18} expected {status:<24} got {got!s:<24} reward={outcome['reward']}  {verdict}"
    )
    return 0 if ok else 1


# --- the controls ----------------------------------------------------------------------------


def noop() -> int:
    """No report at all (what a no-op agent leaves)."""
    write_report(None)
    return expect("noop", run_test_sh(), status="missing_report", graded=True)


def keywords() -> int:
    """Every section, the right paths named, but nothing quoted: keywords, not evidence."""
    oracle = load_oracle()
    named = ", ".join(f"`{n}`" for n in oracle["files"])
    write_report(report("", paths_text=f"The faulty behaviour is implemented in {named}."))
    return expect("keywords", run_test_sh(), status="no_quotation", graded=True)


def wrong_location() -> int:
    """A real quotation from a file the fix does not touch, no oracle path named."""
    oracle, tree = load_oracle(), v.Tree(PRISTINE)
    write_report(report(unrelated_quote(tree, oracle), paths_text=PATHS_TEXT))
    return expect("wrong_location", run_test_sh(), status="wrong_location", graded=True)


def paths_only() -> int:
    """The right paths named in prose, but the quotation is from somewhere else."""
    oracle, tree = load_oracle(), v.Tree(PRISTINE)
    named = ", ".join(f"`{n}`" for n in oracle["files"])
    write_report(
        report(
            unrelated_quote(tree, oracle),
            paths_text=f"The faulty behaviour is implemented in {named}.",
        )
    )
    return expect("paths_only", run_test_sh(), status="wrong_location", graded=True)


def wrong_order() -> int:
    """The right code, but the sections are in the wrong order: the shape is part of the task."""
    oracle, tree = load_oracle(), v.Tree(PRISTINE)
    shuffled = ("cause", "operation", "paths", "chain", "impact")
    write_report(report(gold_quote(tree, oracle), paths_text=PATHS_TEXT, order=shuffled))
    return expect("wrong_order", run_test_sh(), status="sections_out_of_order", graded=True)


def missing_section() -> int:
    """The right code, but a required section is left out."""
    oracle, tree = load_oracle(), v.Tree(PRISTINE)
    kept = tuple(name for name in v.REQUIRED_SECTIONS if name != "chain")
    write_report(report(gold_quote(tree, oracle), paths_text=PATHS_TEXT, order=kept))
    return expect("missing_section", run_test_sh(), status="missing_section", graded=True)


def infra_down() -> int:
    """The grader's own reference tree is gone: ungraded, with no reward, never a graded 0."""
    oracle, tree = load_oracle(), v.Tree(PRISTINE)
    write_report(report(gold_quote(tree, oracle), paths_text=PATHS_TEXT))
    moved = PRISTINE + ".away"
    shutil.move(PRISTINE, moved)
    try:
        outcome = run_test_sh()
    finally:
        shutil.move(moved, PRISTINE)
    return expect("infra_down", outcome, status="ungraded", graded=False)


CONTROLS = {
    "noop": noop,
    "keywords": keywords,
    "wrong_location": wrong_location,
    "paths_only": paths_only,
    "wrong_order": wrong_order,
    "missing_section": missing_section,
    "infra_down": infra_down,
}


def main(argv: list[str]) -> int:
    name = argv[1] if len(argv) > 1 else ""
    if name not in CONTROLS:
        sys.stderr.write(f"usage: controls.py {{{'|'.join(CONTROLS)}}}\n")
        return 2
    try:
        return CONTROLS[name]()
    except CannotBuild as why:
        print(f"  {name:<18} CANNOT BUILD: {why}")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
