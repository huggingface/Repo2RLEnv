from __future__ import annotations

import ast

from repo2rlenv.execution.python_repository import repository_source_files
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


def test_private_generated_test_uses_the_configured_nested_test_directory():
    private = verifier_files(
        {
            "module": "toolz.itertoolz",
            "function_name": "target",
            "generated_test_path": "toolz/tests/test_r2e_generated.py",
        },
        b"def target(x): return x\n",
        "from fut_module import target, reference_target\ndef test_target():\n"
        "    assert target(1) == reference_target(1)\n",
    )
    assert "toolz/tests/test_r2e_generated.py" in private
    assert "tests/test_r2e_generated.py" not in private


def test_nested_tests_are_excluded_before_source_candidate_generation(tmp_path):
    for name in ("lib/core.py", "lib/tests/test_core.py", "lib/test_entry.py", "lib/tests_api.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def example(): pass\n")
    options = R2EOptions(
        source_paths=["lib", "lib/core.py"], test_paths=["lib/tests", "lib/test_entry.py"]
    )
    assert [
        p.relative_to(tmp_path).as_posix() for p in repository_source_files(tmp_path, options)
    ] == ["lib/core.py", "lib/tests_api.py"]


def test_coverage_measures_only_the_configured_generated_tests(monkeypatch):
    import importlib.util
    import sys
    from importlib.resources import files
    from types import SimpleNamespace
    from unittest.mock import Mock

    recorder = Mock()
    # The coverage package belongs to the remote test image, not the CLI runtime.
    monkeypatch.setitem(
        sys.modules, "coverage", SimpleNamespace(Coverage=lambda **kwargs: recorder)
    )
    spec = importlib.util.spec_from_file_location(
        "r2e_coverage_test",
        str(files("repo2rlenv.pipelines.recipes.r2e").joinpath("coverage_driver.py")),
    )
    coverage_driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(coverage_driver)
    plugin = coverage_driver.GeneratedTestCoverage(
        "toolz/dicttoolz.py", "toolz/tests/test_r2e_generated.py"
    )
    for nodeid in (
        "toolz/tests/test_r2e_generated.py::test_get_in[nested]",
        "toolz/tests/test_dicttoolz.py::test_get_in",
        "tests/test_r2e_generated.py::test_get_in",
    ):
        list(plugin.pytest_runtest_protocol(SimpleNamespace(nodeid=nodeid), None))
    recorder.start.assert_called_once_with()
    recorder.stop.assert_called_once_with()
