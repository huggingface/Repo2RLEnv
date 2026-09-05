from __future__ import annotations

from repo2rlenv.log_parsers.pytest_parser import parse_pytest as parse_canonical_pytest
from repo2rlenv.pipelines._pr_runtime_verifier import grade, parse_pytest as parse_runtime_pytest


def test_pytest_parsers_preserve_spaced_parametrized_node_ids():
    log = (
        "tests/test_calc.py::test_eval[1 + 1] PASSED [ 50%]\n"
        "FAILED tests/test_calc.py::test_eval[left - right] - AssertionError: fail\n"
        "tests/test_calc.py::test_eval[expected PASSED value] FAILED [100%]\n"
    )
    expected = {
        "tests/test_calc.py::test_eval[1 + 1]": "PASSED",
        "tests/test_calc.py::test_eval[left - right]": "FAILED",
        "tests/test_calc.py::test_eval[expected PASSED value]": "FAILED",
    }

    assert parse_canonical_pytest(log) == expected
    assert parse_runtime_pytest(log) == expected


def test_runtime_grading_matches_full_spaced_node_id():
    node_id = "tests/test_calc.py::test_eval[1 + 1]"
    status_map = parse_runtime_pytest(f"{node_id} PASSED [100%]\n")

    result = grade([node_id], [], status_map)

    assert result["reward"] == 1.0
    assert result["resolved"] is True
