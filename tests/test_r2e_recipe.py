from __future__ import annotations

import ast

from repo2rlenv.pipelines.recipes.r2e.extract import dependency_slice, stub
from repo2rlenv.pipelines.recipes.r2e.reference import verifier_files
from repo2rlenv.pipelines.recipes.r2e.worker import branch_evidence
from repo2rlenv.spec.options import EquivalenceTestsOptions, parse_options
from repo2rlenv.spec.recipe_options import R2EOptions


def test_dependency_slice_follows_helpers_and_constants_without_unrelated_code():
    source = "import math\nSCALE = 2\nUNRELATED = 999\ndef helper(x):\n    return math.ceil(x) * SCALE\ndef target(x):\n    return helper(x)\ndef unrelated():\n    return UNRELATED\n"
    result = dependency_slice(source, "target")
    assert "def helper" in result and "SCALE = 2" in result and "import math" in result
    assert "UNRELATED" not in result and "def unrelated" not in result
    draft = stub(source, "target", "Return the transformed input.")
    target = next(
        node
        for node in ast.parse(draft).body
        if isinstance(node, ast.FunctionDef) and node.name == "target"
    )
    assert isinstance(target.body[-1], ast.Raise)
    assert "def helper" in draft


def test_branch_feedback_is_scoped_to_target_and_absence_is_not_full_coverage():
    candidate = {"path": "library.py", "line": 10, "end_line": 20}
    report = {
        "files": {
            "library.py": {
                "executed_lines": [11, 12, 101],
                "executed_branches": [[12, 13], [100, 101]],
                "missing_branches": [[12, 15]],
            }
        }
    }
    result = branch_evidence(report, candidate)
    assert result["branch_coverage"] == 0.5
    assert result["covered_branches"] == [[12, 13]]
    assert branch_evidence({"files": {}}, candidate)["branch_coverage"] == 0


def test_r2e_private_helpers_preserve_future_imports_and_native_options_stay_native():
    code = "from __future__ import annotations\nimport unittest\nfrom fut_module import value, reference_value\nclass TestValue(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(value(3), reference_value(3))\n"
    private = verifier_files(
        {"module": "library.values", "function_name": "value"},
        b"def value(x): return x * 17\n",
        code,
    )
    assert set(private) == {"fut_module.py", ".r2e_reference.py", "tests/test_r2e_generated.py"}
    tree = ast.parse(private["tests/test_r2e_generated.py"])
    assert tree.body[0].module == "__future__"
    assert b"library._r2e_reference" in private["fut_module.py"]
    assert isinstance(parse_options("equivalence_tests", {}), EquivalenceTestsOptions)
    assert isinstance(
        parse_options(
            "equivalence_tests",
            {"source_paths": ["library"], "test_paths": ["tests"]},
            recipe="r2e",
        ),
        R2EOptions,
    )
