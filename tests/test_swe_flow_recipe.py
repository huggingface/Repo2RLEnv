from __future__ import annotations

import ast
import json
from types import SimpleNamespace

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


def test_specification_receives_the_same_public_contract_as_the_skeleton(monkeypatch, tmp_path):
    from repo2rlenv.pipelines.recipes.swe_flow import author as module

    calls = []
    documents = {
        "functions": [
            {
                "node_id": "module:f",
                "docstring": "Return the original initial value before subsequent differences.",
            }
        ]
    }

    def complete(*args, **kwargs):
        calls.append(kwargs)
        response = (
            documents
            if len(calls) == 1
            else {"markdown": "Implement f with its public initial-value convention. " * 3}
        )
        return SimpleNamespace(content=json.dumps(response))

    monkeypatch.setattr("repo2rlenv.campaigns.structured.metered_complete", complete)
    candidate = {
        "functions": {"module:f": {"name": "f"}},
        "test_evidence": {"initial": "expected first output equals initial"},
    }
    result, _ = module.author(
        candidate, None, None, tmp_path, operation_prefix="owned-test", resume=False
    )
    supplied = json.loads(calls[1]["user"])
    assert supplied["public_docstrings"] == documents
    assert supplied["test_evidence"] == candidate["test_evidence"]
    assert supplied["scheduled_functions"] == candidate["functions"]
    assert result["module:f"] == documents["functions"][0]["docstring"]


def test_excluded_schedules_are_not_executed_or_charged_again(monkeypatch, tmp_path):
    import hashlib

    from repo2rlenv.pipelines.recipes.swe_flow import worker
    from repo2rlenv.spec.input import RepoSpec
    from repo2rlenv.spec.recipe_options import ReconstructionOptions

    base = tmp_path / "source"
    base.mkdir()
    (base / "module.py").write_text("def f():\n    return 1\n")
    schedules = [{"step": i, "nodes_to_develop": ["f"]} for i in (1, 2)]
    repo = RepoSpec(url="https://github.com/example/owned-fixture", ref="abc")
    skipped_id = hashlib.sha256(
        json.dumps(
            {"repo": repo.url, "ref": "abc", "schedule": schedules[0]}, sort_keys=True
        ).encode()
    ).hexdigest()[:20]
    options = ReconstructionOptions(
        source_paths=["module.py"],
        test_paths=["tests"],
        target=1,
        max_candidates=1,
        exclude_candidate_ids=[skipped_id],
    )
    executed = []

    def run_tests(image, options, destination, **kwargs):
        destination.mkdir(parents=True, exist_ok=True)
        if "instrumentation" in kwargs:
            (destination / "traces.json").write_text("[]")
        if "replacements" in kwargs:
            executed.append(destination.parent.name)
        return SimpleNamespace(returncode=0, passed=["owned_test"])

    monkeypatch.setattr(
        worker,
        "bootstrap_snapshot",
        lambda *args: (SimpleNamespace(image_digest="owned-image", ref="abc"), base),
    )
    monkeypatch.setattr(worker, "test_image", run_tests)
    monkeypatch.setattr(worker, "function_catalog", lambda *args: {"f": {"path": "module.py"}})
    monkeypatch.setattr(worker, "development_schedule", lambda *args: schedules)
    monkeypatch.setattr(worker, "skeletonize", lambda *args, **kwargs: "def f(): ...\n")
    monkeypatch.setattr(
        worker, "execution_contrast", lambda *args: {"FAIL_TO_PASS": ["owned_test"]}
    )
    monkeypatch.setattr(worker, "test_excerpts", lambda *args: {})
    result = worker.generate(repo, options, tmp_path / "generation")
    assert result["attempted"] == 1
    assert [row["schedule"]["step"] for row in result["candidates"]] == [2]
    assert executed == [result["candidates"][0]["id"]]
    assert skipped_id not in executed
