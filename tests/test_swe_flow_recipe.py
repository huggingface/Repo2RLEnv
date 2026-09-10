from __future__ import annotations

import ast

from repo2rlenv.pipelines.recipes.swe_flow.schedule import (
    development_schedule,
    function_catalog,
    skeletonize,
)


def test_schedule_groups_identical_dependencies_and_introduces_each_function_once():
    traces = [
        {"test_id": "t3", "core_nodes": ["a", "b"], "target_nodes": ["b"], "edges": [["b", "a"]]},
        {"test_id": "t1", "core_nodes": ["a"], "target_nodes": ["a"], "edges": []},
        {"test_id": "t2", "core_nodes": ["a"], "target_nodes": ["a"], "edges": []},
    ]
    steps = development_schedule(traces, {"a": {}, "b": {}})
    assert len(steps) == 2
    assert steps[0]["test_ids"] == ["t1", "t2"]
    assert steps[0]["nodes_to_develop"] == ["a"]
    assert steps[1]["nodes_to_develop"] == ["b"]
    assert steps[1]["previous_tests"] == ["t1", "t2"]
    assert steps[1]["edges"] == [("b", "a")]


def test_skeleton_preserves_interface_removes_new_dependency_and_keeps_unrelated_code(tmp_path):
    source = "def helper(x):\n    return x * 7919\n\ndef entry(x, y=1):\n    return helper(x) + y\n\ndef existing():\n    return 'preserved'\n"
    (tmp_path / "module.py").write_text(source)
    catalog = function_catalog(tmp_path, ["module.py"])
    entry = next(key for key, value in catalog.items() if value["name"] == "entry")
    helper = next(key for key, value in catalog.items() if value["name"] == "helper")
    draft = skeletonize(
        source,
        path="module.py",
        schedule={"target_nodes": [entry], "dependent_nodes": [helper]},
        docstrings={entry: "Return the specified transformed value."},
    )
    tree = ast.parse(draft)
    assert [node.name for node in tree.body] == ["entry", "existing"]
    assert ast.get_docstring(tree.body[0]) == "Return the specified transformed value."
    assert ast.unparse(tree.body[0].args) == "x, y=1"
    assert ast.literal_eval(tree.body[0].body[-1].value) is Ellipsis
    assert "7919" not in draft
    assert "preserved" in draft
