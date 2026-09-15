"""Minimum execution evidence for numerical and compiled-behavior controls."""

from __future__ import annotations

import json
import re
from pathlib import Path
from xml.etree import ElementTree

from repo2rlenv.quality.loop.artifacts import digest
from repo2rlenv.quality.loop.models import TrialRecord

BEHAVIOR_FOCI = frozenset({"model_behavior", "compiled_execution"})
_LIMIT = 16 * 1024 * 1024
_IMPORT_ERROR = re.compile(
    r"(?m)^(?:E\s+)?(?:builtins\.)?"
    r"(?:SyntaxError|IndentationError|TabError|ImportError|ModuleNotFoundError)(?::|$)"
)


def _read(path: Path) -> str:
    if (
        any(part.is_symlink() for part in (path, *path.parents))
        or not path.is_file()
        or path.stat().st_size > _LIMIT
    ):
        raise ValueError("Behavioral probe evidence must be bounded regular files")
    return path.read_text()


def behavior_files(trial: TrialRecord) -> dict[str, str | None]:
    """Bind structured verifier results when an owned attempt is first recorded."""
    root = Path(trial.result).parent
    files = {}
    for relative in ("verifier/result.json", "verifier/results.xml"):
        path = root / relative
        if path.exists() or path.is_symlink():
            _read(path)
            files[str(path)] = digest(path)
        else:
            files[str(path)] = None
    return files


def _execution_problem(trial: TrialRecord) -> str | None:
    from repo2rlenv.quality.labels import _trial_evidence

    result = Path(trial.result)
    journal = result.parents[4] / "probe-attempts" / f"{result.parents[2].name}.json"
    attempt = json.loads(_read(journal))
    if attempt["trial"] != trial.model_dump(mode="json"):
        raise ValueError("Behavioral probe differs from its recorded attempt")
    _trial_evidence(trial, attempt["parent_hash"])
    log = result.parent / "agent/oracle.txt"
    if attempt["files"].get(str(log)) != digest(log):
        raise ValueError("Behavioral probe audit log changed after collection")
    changes = [
        json.loads(line.removeprefix("__QUALITY_PROBE_CHANGED_FILES__ "))
        for line in _read(log).splitlines()
        if line.startswith("__QUALITY_PROBE_CHANGED_FILES__ ")
    ]
    if len(changes) != 1 or not changes[0]:
        raise ValueError("A collected-source mutation audit is required")
    for name, change in changes[0].items():
        before, after = change["before"], change["after"]
        if (
            name.endswith(".py")
            and after is not None
            and after["syntax_sha256"] is None
            and (before is None or before["syntax_sha256"] is not None)
        ):
            return f"mutation introduced unparseable Python in {name}"
    files = behavior_files(trial)
    if attempt.get("behavior_files") != files or any(value is None for value in files.values()):
        raise ValueError("Checksum-bound structured verifier execution evidence is unavailable")
    summary = json.loads(_read(result.parent / "verifier/result.json"))
    if summary.get("returncode") != 1:
        return "verifier did not report completed failing test execution"
    xml = _read(result.parent / "verifier/results.xml")
    if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise ValueError("Verifier XML cannot contain document or entity declarations")
    report = ElementTree.fromstring(xml)
    for case in report.iter("testcase"):
        failure = case.find("failure")
        if failure is None:
            continue
        description = "\n".join(
            [failure.get("type", ""), failure.get("message", ""), failure.text or ""]
        )
        if not _IMPORT_ERROR.search(description):
            return None
    return "verifier reports only collection, setup, import or syntax failures; no behavioral rejection"


def behavioral_failure(trial: TrialRecord, success: float = 1.0) -> str | None:
    """Reward zero alone cannot establish a required model/compiled counterexample.

    This is an execution floor, not proof that an assertion measures the claimed
    numerical contract. The reviewer still judges that relationship. Generic
    controls and valid alternatives retain their existing policy.
    """
    if (
        trial.role != "probe"
        or trial.probe is None
        or trial.probe.kind != "wrong_solution"
        or trial.probe.focus not in BEHAVIOR_FOCI
        or not trial.probe_installed
        or trial.reward is None
        or trial.reward >= success
        or trial.exception_type is not None
        or trial.agent_exit_code not in {None, 0}
    ):
        return None
    try:
        problem = _execution_problem(trial)
    except (ValueError, OSError, KeyError, IndexError, TypeError, ElementTree.ParseError) as exc:
        problem = f"behavioral execution evidence needs diagnosis ({type(exc).__name__})"
    return f"Probe {trial.probe.name}: {problem}" if problem else None
