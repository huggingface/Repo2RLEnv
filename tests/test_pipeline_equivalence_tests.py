"""equivalence_tests — helpers, builders, and contract conformance.

The function extractor is exercised in test_function_extractor.py.
Here we cover the pipeline's pure-Python pieces:

  - _extract_test_section (LLM response parsing)
  - test_uses_both_names (test must reference both `name` and `reference_name`)
  - _stub_module / _oracle_module (task_module.py shape)
  - _rename_function_source (rename in source text)
  - _make_two_file_diff (gold patch shape)
  - Pipeline contract (requires_bootstrap, missing-bootstrap rejection)
"""

from __future__ import annotations

import pytest

from repo2rlenv.pipelines._function_extractor import FunctionCandidate
from repo2rlenv.pipelines.code_instruct import make_solution_diff
from repo2rlenv.pipelines.equivalence_tests import (
    EquivalenceTestsPipeline,
    _extract_test_section,
    _oracle_module,
    _rename_function_source,
    _stub_module,
    uses_both_names,
)
from repo2rlenv.spec.options import EquivalenceTestsOptions


def _make_candidate(
    name: str = "add",
    arg_names: tuple[str, ...] = ("x", "y"),
    source: str = 'def add(x, y):\n    """Sum."""\n    return x + y\n',
    docstring: str = "Sum.",
    body_loc: int = 2,
) -> FunctionCandidate:
    return FunctionCandidate(
        relative_path="src/foo.py",
        name=name,
        lineno=1,
        end_lineno=3,
        body_loc=body_loc,
        source=source,
        docstring=docstring,
        arg_names=arg_names,
    )


# ---------------------------------------------------------------------------
# _extract_test_section
# ---------------------------------------------------------------------------


def test_extracts_test_section_with_fence():
    text = (
        "[Test]\n"
        "```python\n"
        "from task_module import add, reference_add\n"
        "def test_add():\n"
        "    assert add(1, 2) == reference_add(1, 2)\n"
        "```\n"
    )
    parsed = _extract_test_section(text)
    assert parsed is not None
    assert "from task_module import add" in parsed.code
    assert not parsed.code.startswith("```")


def test_extracts_test_section_without_fence():
    text = (
        "[Test]\n"
        "from task_module import add, reference_add\n"
        "def test_add():\n"
        "    assert add(1, 2) == reference_add(1, 2)\n"
    )
    parsed = _extract_test_section(text)
    assert parsed is not None
    assert "def test_add" in parsed.code


def test_extracts_test_section_case_insensitive():
    text = (
        "[test]\n"
        "from task_module import add, reference_add\n"
        "def test_x():\n"
        "    assert add(1, 1) == reference_add(1, 1)\n"
    )
    parsed = _extract_test_section(text)
    assert parsed is not None


def test_extract_test_section_returns_none_when_missing():
    text = "Here is some content but no test section."
    assert _extract_test_section(text) is None


def test_extract_test_section_stops_at_next_section():
    text = (
        "[Test]\nfrom task_module import x, reference_x\ndef test_x(): pass\n[Notes]\nignore me\n"
    )
    parsed = _extract_test_section(text)
    assert parsed is not None
    assert "ignore me" not in parsed.code


# ---------------------------------------------------------------------------
# test_uses_both_names
# ---------------------------------------------------------------------------


def test_uses_both_names_happy_path():
    code = (
        "from task_module import add, reference_add\n"
        "def test_add():\n"
        "    assert add(1, 2) == reference_add(1, 2)\n"
    )
    assert uses_both_names(code, "add")


def test_uses_both_names_missing_candidate():
    """Test that imports only the reference is trivial — reject it."""
    code = (
        "from task_module import reference_add\n"
        "def test_add():\n"
        "    assert reference_add(1, 2) == 3\n"
    )
    assert not uses_both_names(code, "add")


def test_uses_both_names_missing_reference():
    code = "from task_module import add\ndef test_add():\n    assert add(1, 2) == 3\n"
    assert not uses_both_names(code, "add")


def test_uses_both_names_missing_import():
    code = "def test_add():\n    assert add(1, 2) == reference_add(1, 2)\n"
    assert not uses_both_names(code, "add")


# ---------------------------------------------------------------------------
# _rename_function_source
# ---------------------------------------------------------------------------


def test_rename_simple_def():
    src = 'def add(x, y):\n    """Sum."""\n    return x + y\n'
    out = _rename_function_source(src, "add", "reference_add")
    assert out.startswith("def reference_add(x, y):")


def test_rename_preserves_decorators():
    src = "@staticmethod\ndef add(x, y):\n    return x + y\n"
    out = _rename_function_source(src, "add", "reference_add")
    assert "@staticmethod" in out
    assert "def reference_add" in out


def test_rename_only_renames_def_line():
    """Internal recursive calls keep the old name — known v0.7 limitation."""
    src = "def fact(n):\n    return 1 if n <= 1 else n * fact(n - 1)\n"
    out = _rename_function_source(src, "fact", "reference_fact")
    assert "def reference_fact" in out
    # The recursive call still says `fact(n - 1)` — that's the documented v0.7 trade-off
    assert "fact(n - 1)" in out


# ---------------------------------------------------------------------------
# _stub_module / _oracle_module
# ---------------------------------------------------------------------------


def test_stub_module_has_stubbed_candidate():
    c = _make_candidate()
    out = _stub_module(c)
    assert "def reference_add(x, y):" in out
    assert "def add(x, y):" in out
    assert "raise NotImplementedError" in out


def test_oracle_module_has_two_definitions():
    c = _make_candidate()
    out = _oracle_module(c)
    assert "def reference_add(x, y):" in out
    assert "def add(x, y):" in out
    assert "raise NotImplementedError" not in out


# ---------------------------------------------------------------------------
# Gold patch shape — carries ONLY task_module.py (issue #54)
# ---------------------------------------------------------------------------


def test_solution_diff_has_single_header():
    diff = make_solution_diff(task_module_code="def add(x, y):\n    return x + y\n")
    assert diff.count("diff --git ") == 1
    assert "diff --git a/task_module.py b/task_module.py" in diff


def test_solution_diff_excludes_test_file():
    """Regression for #54: the equivalence test ships under tests/, not in the
    gold patch — otherwise only the OracleAgent could reach it."""
    diff = make_solution_diff(task_module_code="x = 1\n")
    assert "test_r2e" not in diff
    assert diff.count("new file mode") == 1
    assert diff.count("--- /dev/null") == 1


def test_solution_diff_hunk_line_counts():
    diff = make_solution_diff(task_module_code="line1\nline2\nline3\n")
    assert "@@ -0,0 +1,3 @@" in diff


# ---------------------------------------------------------------------------
# Pipeline contract
# ---------------------------------------------------------------------------


def test_equivalence_tests_requires_bootstrap_attr():
    assert EquivalenceTestsPipeline.requires_bootstrap is True


def test_equivalence_tests_rejects_missing_bootstrap():
    from repo2rlenv.spec.input import (
        GenerationInput,
        LLMSpec,
        OutputSpec,
        PipelineName,
        PipelineSpec,
        RepoSpec,
    )

    gen_input = GenerationInput(
        repo=RepoSpec(url="huggingface/trl"),
        pipeline=PipelineSpec(name=PipelineName.EQUIVALENCE_TESTS, options={}),
        llm=LLMSpec(provider="anthropic", model="claude-sonnet-4-6"),
        output=OutputSpec(destination="./out", org="x", dataset_name="y"),
    )
    with pytest.raises(RuntimeError, match="requires a BootstrapResult"):
        EquivalenceTestsPipeline(gen_input, EquivalenceTestsOptions(), bootstrap=None)


def test_equivalence_tests_options_defaults():
    opts = EquivalenceTestsOptions()
    assert opts.limit == 50
    assert opts.min_loc == 5
    assert opts.max_loc == 60
    assert opts.require_test_fails_with_stub is True
    assert opts.require_test_passes_with_oracle is True
