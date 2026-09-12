from __future__ import annotations

import pytest

pytest.importorskip("pyflakes")

from repo2rlenv.pipelines.recipes.swe_smith.issue import IssueReport, issue_violations


def violations(issue):
    return issue_violations(
        IssueReport(issue=issue, reason="Observed behavior"),
        {"contrast": {"FAIL_TO_PASS": ["tests.test_api::test_result"]}},
    )


def test_reproducer_typo_is_rejected_before_export():
    result = violations('```python\nimport math\napprox = math.pi\nprint(f"{apprx}")\n```')
    assert len(result) == 1
    assert "undefined name 'apprx'" in result[0]


def test_valid_reproducer_is_parsed_without_importing_or_executing_it():
    assert not violations(
        "```python\nimport nonexistent_repository_module as api\n"
        "import unused_module\nresult = api.function()\nprint(result)\n```"
    )


def test_invalid_python_syntax_is_actionable():
    assert "invalid syntax on line 1" in violations("```py\nvalue = (\n```")[0]


def test_private_test_provenance_is_not_part_of_a_bug_report():
    assert violations("The test evidence requires less than one percent error.")
