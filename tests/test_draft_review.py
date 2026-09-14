from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from repo2rlenv.quality.draft_review import review_draft


def test_concrete_reference_defect_is_returned_to_materialization(monkeypatch, tmp_path):
    draft = SimpleNamespace(
        instruction="Repair /workspace/tool.sh so it writes output.txt.",
        tests_python="assert open('output.txt').read() == 'ok'",
        solution_shell="echo ok > output.txt",
        environment_setup="",
        environment_files=[],
    )
    issue = {
        "category": "reference",
        "severity": "blocking",
        "problem": "Reference creates output but leaves requested tool broken.",
        "repair": "Install the fixed tool and invoke it.",
        "evidence": [{"path": "solution/solve.sh", "quote": "echo ok > output.txt"}],
    }
    monkeypatch.setattr(
        "repo2rlenv.campaigns.structured.metered_complete",
        lambda *args, **kwargs: SimpleNamespace(
            content=json.dumps({"summary": "Reference mismatch", "issues": [issue]})
        ),
    )
    with pytest.raises(ValueError, match="Install the fixed tool"):
        review_draft(
            draft,
            model=None,
            ledger=None,
            directory=tmp_path,
            operation_id="review:fixture",
            resume=False,
        )
    assert json.loads((tmp_path / "decision.json").read_text())["issues"] == [issue]
