"""Strict test evidence used by repository recipes and their exported verifiers."""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree


@dataclass(frozen=True)
class TestResults:
    __test__ = False
    statuses: dict[str, str]
    returncode: int

    @property
    def passed(self) -> set[str]:
        return {name for name, status in self.statuses.items() if status == "passed"}


def parse_junit(text: str, *, returncode: int) -> TestResults:
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("JUnit must not contain a DTD or entity declaration")
    if returncode not in {0, 1}:
        raise ValueError(f"Test runner did not complete normally: exit {returncode}")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ValueError("Invalid JUnit report") from exc
    if root.tag not in {"testsuite", "testsuites"}:
        raise ValueError("Expected a JUnit testsuite or testsuites root")
    statuses = {}
    for case in root.iter("testcase"):
        name, classname = case.get("name"), case.get("classname")
        if not name or not classname:
            raise ValueError("Test cases require a name and class identity")
        identity = f"{classname}::{name}"
        if identity in statuses:
            raise ValueError(f"Duplicate test identity: {identity}")
        if case.find("error") is not None:
            status = "error"
        elif case.find("failure") is not None:
            status = "failed"
        elif case.find("skipped") is not None:
            status = "skipped"
        else:
            status = "passed"
        statuses[identity] = status
    if not statuses:
        raise ValueError("Empty test result is not execution evidence")
    if returncode == 0 and any(status in {"failed", "error"} for status in statuses.values()):
        raise ValueError("JUnit failures contradict the runner exit code")
    return TestResults(statuses, returncode)


def execution_contrast(healthy: TestResults, defective: TestResults) -> dict[str, list[str]]:
    if healthy.returncode != 0 or not healthy.passed:
        raise ValueError("Reference must execute successfully with passing tests")
    if healthy.statuses.keys() != defective.statuses.keys():
        raise ValueError("Mutation changed test collection; contrast is not comparable")
    failing = sorted(
        name for name in healthy.passed if defective.statuses[name] in {"failed", "error"}
    )
    if not failing or defective.returncode != 1:
        raise ValueError("No intended fail-to-pass contrast")
    return {"FAIL_TO_PASS": failing, "PASS_TO_PASS": sorted(healthy.passed - set(failing))}
