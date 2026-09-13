"""Private assertion evidence cannot be crowded out by generic task machinery."""

from __future__ import annotations

import json

import pytest

from repo2rlenv.quality.loop.artifacts import digest
from repo2rlenv.quality.loop.context import EvidenceContext
from repo2rlenv.quality.loop.models import ReadRequest, TrialRecord


@pytest.mark.parametrize("selected_class", [False, True])
def test_selected_assertions_precede_long_reference_patch_and_grading_driver(
    tmp_path, selected_class
):
    task = tmp_path / "task"
    task.mkdir()
    # Match the real task's large core contract and generic top-level files.
    core = {
        "instruction.md": "Choose the minimum rank meeting the error threshold.\n" + "I" * 3400,
        "task.toml": "# " + "T" * 1600,
        "environment/Dockerfile": "# " + "D" * 3600,
        "solution/solve.sh": "# " + "S" * 2900,
        "tests/test.sh": "#!/bin/sh\npython /tests/grade.py\n",
        "solution/reference.patch": "+reference implementation\n" * 1200,
        "tests/grade.py": "# generic grading driver\n" * 1200,
        "tests/test_results.py": "# generic result parsing\n" * 1200,
    }
    for name, text in core.items():
        path = task / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    private_key = "tests/source/tests/tasksmith_behavior.py"
    private = task / private_key
    private.parent.mkdir(parents=True)
    assertions = (
        "class TestConversion:\n"
        "    def test_minimum_rank(self):\n"
        "        chosen_rank, error, lower_rank_error = run_conversion()\n"
        "        assert chosen_rank == 2\n"
        "        assert error <= threshold\n"
        "        assert lower_rank_error > threshold\n"
    )
    content = "# fixture context\n" * 250 + assertions
    # A whole-file selection has a long tail; a class selector must extract the
    # chosen class even when the unrelated rest of the module is much larger.
    content += "# other cases\n" * (2000 if selected_class else 850)
    private.write_text(content)
    selector = "tests/tasksmith_behavior.py" + ("::TestConversion" if selected_class else "")
    (task / "tests/contract.json").write_text(
        json.dumps(
            {
                "test_paths": [selector],
                "expected_passes": ["tests.tasksmith_behavior.TestConversion::test_minimum_rank"],
                "submitted_files": ["src/" + "m" * 80 + str(index) + ".py" for index in range(90)],
            }
        )
    )
    result = tmp_path / "trial/result.json"
    result.parent.mkdir()
    result.write_text("{}")
    trial = TrialRecord(
        role="oracle",
        bundle_hash="sha256:" + "a" * 64,
        result=str(result),
        result_sha256=digest(result),
        agent="oracle",
        model=None,
        reward=1.0,
        exception_type=None,
        binding="receipt",
    )

    context = EvidenceContext(task, [trial], limit=100000)

    assert assertions in context.documents[private_key]
    assert assertions in private.read_text()
    assert list(context.documents).index("tests/contract.json") < list(context.documents).index(
        private_key
    )
    task_characters = sum(
        len(text) for key, text in context.documents.items() if not key.startswith("evidence/")
    )
    assert task_characters <= 35000
    assert sum(map(len, context.documents.values())) <= context.limit
    payload = json.loads(context.payload())
    assert private_key in payload["documents"]
    inventory = {item["path"] for item in context.inventory}
    assert {private_key, "solution/reference.patch", "tests/grade.py"} <= inventory

    # Lower-priority machinery stays addressable, and adding an excerpt cannot
    # alter the already supplied assertion text or accept the wrong source key.
    before = dict(context.documents)
    context.read_more([ReadRequest(path="tests/grade.py", query=None, start_line=1, end_line=2)])
    assert context.documents["tests/grade.py:L1-L2"] == "# generic grading driver\n" * 2
    assert all(context.documents[key] == text for key, text in before.items())
    with pytest.raises(ValueError, match="not in evidence inventory"):
        context.read_more(
            [ReadRequest(path="tests/tasksmith_behavior.py", query=None, start_line=1, end_line=2)]
        )
