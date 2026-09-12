from __future__ import annotations

import pytest

pytest.importorskip("libcst")

from repo2rlenv.pipelines.recipes.swe_smith.mutations import generate_mutations


def test_mutations_preserve_unrelated_code_and_comments():
    source = "# module comment\nLIMIT = 5\n\ndef différence(x):\n    # retain this comment\n    return x + 1\n\ndef untouched():\n    return 'literal'\n"
    mutations = generate_mutations(source, "library.py", seed=4)
    assert mutations
    for mutation in mutations:
        assert mutation.source.startswith("# module comment\nLIMIT = 5\n")
        assert "# retain this comment" in mutation.source
        assert mutation.source.endswith("def untouched():\n    return 'literal'\n")
        assert mutation.entity == "différence"
    assert generate_mutations(source, "library.py", seed=4) == mutations
    assert len({mutation.id for mutation in mutations}) == len(mutations)


def test_condition_inversion_keeps_boolean_precedence():
    source = "def f(a, b):\n    if a and b:\n        return a\n    return b\n"
    mutations = generate_mutations(source, "module.py")
    inversion = next(item for item in mutations if item.operator == "invert_condition")
    assert "if not (a and b):" in inversion.source


def test_global_expressions_are_not_mutated():
    assert not generate_mutations("VERSION = 1 + 2\n", "module.py")
