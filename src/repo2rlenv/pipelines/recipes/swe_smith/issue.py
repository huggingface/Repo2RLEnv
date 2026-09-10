"""SWE-smith's test-evidence issue generation, through the shared metered client."""

from __future__ import annotations

import ast
import json
import re
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from repo2rlenv.campaigns.budget import BudgetLedger
from repo2rlenv.campaigns.llm import metered_complete
from repo2rlenv.quality.python_evidence import test_excerpts
from repo2rlenv.spec.input import LLMSpec


class IssueReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue: str
    reason: str


def _reproducer_errors(markdown: str) -> list[str]:
    """Check self-contained Python examples without executing generated code."""
    from pyflakes.checker import Checker
    from pyflakes.messages import UndefinedLocal, UndefinedName

    errors = []
    blocks = re.findall(r"^```(?:python|py)\s*\n(.*?)^```\s*$", markdown, re.M | re.S)
    for index, source in enumerate(blocks, start=1):
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            errors.append(f"Python example {index} has invalid syntax on line {exc.lineno}")
            continue
        for message in Checker(tree).messages:
            if isinstance(message, (UndefinedName, UndefinedLocal)):
                errors.append(
                    f"Python example {index}, line {message.lineno}: "
                    + message.message % message.message_args
                )
    return errors


def issue_violations(report: IssueReport, candidate: dict) -> list[str]:
    issues = []
    text = report.issue
    if not text.strip():
        issues.append("No supported issue: " + report.reason)
    if len(text) > 4000:
        issues.append("Use a concise bug report under 4000 characters")
    names = {
        part
        for identity in candidate["contrast"]["FAIL_TO_PASS"]
        for part in identity.replace("::", ".").split(".")
        if part.startswith("test_") or part.endswith("Tests")
    }
    if any(name in text for name in names):
        issues.append("Remove private test function and class names")
    if any(
        phrase in text.lower()
        for phrase in (
            "test suite",
            "test-suite",
            "failing test",
            "failed test",
            "tests assert",
            "test shows",
            "test evidence",
        )
    ):
        issues.append("Describe observable behavior without referring to the supplied tests")
    issues.extend(_reproducer_errors(text))
    return issues


def issue_context(generation: Path, candidate: dict) -> str:
    """Select failing test methods and imports without executing repository code."""
    base = generation / "base"
    excerpts = test_excerpts(base, candidate["contrast"]["FAIL_TO_PASS"])
    log = (generation / "candidates" / candidate["id"] / "defective" / "stdout.txt").read_text()
    context = json.dumps({"test_source": excerpts, "test_execution": log}, ensure_ascii=False)
    if len(context) > 100_000:
        raise ValueError("Issue evidence exceeds the bounded context; select a narrower profile")
    return context


def write_issue(
    generation: Path,
    candidate: dict,
    model: LLMSpec,
    ledger: BudgetLedger,
    receipt: Path,
    *,
    operation_id: str,
    reservation_usd: str = "0.50",
    max_attempts: int = 2,
    review_feedback: list[str] | None = None,
    resume: bool = False,
) -> IssueReport:
    context = issue_context(generation, candidate)
    if max_attempts not in range(1, 4):
        raise ValueError("Issue generation allows one to three explicit attempts")
    feedback = (
        "\nAddress these review findings: " + json.dumps(review_feedback) if review_feedback else ""
    )
    for attempt in range(1, max_attempts + 1):
        response = metered_complete(
            model,
            ledger=ledger,
            operation_id=operation_id if attempt == 1 else f"{operation_id}:attempt-{attempt}",
            reservation_usd=reservation_usd,
            receipt=receipt
            if attempt == 1
            else receipt.with_stem(f"{receipt.stem}-attempt-{attempt}"),
            system=files(__package__).joinpath("issue_prompt.md").read_text(),
            user=context + feedback,
            max_tokens=4096,
            response_schema=IssueReport.model_json_schema(),
            resume=resume,
        )
        try:
            result = IssueReport.model_validate_json(response.content)
            violations = issue_violations(result, candidate)
            if not violations:
                return result
            feedback = (
                "\nRevise your previous response to address these validation errors: "
                + json.dumps(violations)
            )
        except ValueError:
            feedback = (
                "\nReturn a valid JSON object with exactly the string fields issue and reason."
            )
    raise ValueError("Issue generation exhausted its bounded attempts: " + feedback)
