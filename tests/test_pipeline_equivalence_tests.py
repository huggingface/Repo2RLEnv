"""equivalence_tests — helpers, builders, and contract conformance.

The function extractor is exercised in test_function_extractor.py.
Here we cover the pipeline's pure-Python pieces:

  - _extract_test_section (LLM response parsing)
  - test_uses_both_names (test must reference both `name` and `reference_name`)
  - _stub_module / _oracle_module (task_module.py shape)
  - _rename_function_source (rename in source text)
  - build_equivalence_dockerfile + stub→oracle gold patch (solvability)
  - Pipeline contract (requires_bootstrap, missing-bootstrap rejection)
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from repo2rlenv.pipelines._eval_script import make_unified_diff
from repo2rlenv.pipelines._function_extractor import FunctionCandidate
from repo2rlenv.pipelines.equivalence_tests import (
    EquivalenceTestsPipeline,
    _extract_test_section,
    _oracle_module,
    _rename_function_source,
    _stub_module,
    build_equivalence_dockerfile,
    uses_both_names,
)
from repo2rlenv.spec.options import EquivalenceTestsOptions


def _candidate() -> FunctionCandidate:
    return FunctionCandidate(
        relative_path="src/calc.py",
        name="add",
        lineno=1,
        end_lineno=2,
        body_loc=1,
        source="def add(x, y):\n    return x + y\n",
        docstring="",
        arg_names=("x", "y"),
    )


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
# Stub bake + stub→oracle gold patch (issue #54 — solvable by non-oracle agents)
# ---------------------------------------------------------------------------


def test_dockerfile_bakes_stub_module():
    """Every agent must start with the stub (reference_<name> present) — so it's
    baked into the image, not packed into the OracleAgent-only solution/."""
    stub = _stub_module(_candidate())
    df = build_equivalence_dockerfile("local/img:abc", stub)
    assert "FROM local/img:abc" in df
    assert "base64 -d > /workspace/task_module.py" in df
    # reference_<name> + stub must both be in the baked payload
    assert "reference_add" in stub
    assert "raise NotImplementedError" in stub


def test_gold_patch_is_stub_to_oracle_modify_diff():
    """Gold patch transforms the baked stub into the oracle (not a new file)."""
    cand = _candidate()
    diff = make_unified_diff(_stub_module(cand), _oracle_module(cand), "task_module.py")
    assert "diff --git a/task_module.py b/task_module.py" in diff
    assert "new file mode" not in diff  # modify, not create
    assert "raise NotImplementedError" in diff  # the stub line is removed


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_oracle_patch_applies_to_baked_stub(tmp_path: Path):
    """End-to-end solvability: applying the gold patch to the baked stub via
    `git apply` (exactly what solve.sh does) yields the oracle module."""
    cand = _candidate()
    stub = _stub_module(cand)
    oracle = _oracle_module(cand)
    diff = make_unified_diff(stub, oracle, "task_module.py")

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "task_module.py").write_text(stub)  # what the image bakes
    (tmp_path / "patch.diff").write_text(diff)
    subprocess.run(["git", "apply", "patch.diff"], cwd=tmp_path, check=True)

    got = (tmp_path / "task_module.py").read_text()
    assert got.rstrip("\n") == oracle.rstrip("\n")
    assert "raise NotImplementedError" not in got  # stub really replaced


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
