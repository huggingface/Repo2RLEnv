from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from repo2rlenv.pipelines.recipes.endless_terminals import recipe
from repo2rlenv.pipelines.recipes.endless_terminals.sampler import sample_inputs
from repo2rlenv.pipelines.recipes.terminal import templates


def test_native_category_complexity_context_sampling(tmp_path):
    source = tmp_path / "sampler.json"
    source.write_text(json.dumps({"seed": 24, "count": 20}))
    seeds = sample_inputs(source)
    assert seeds == sample_inputs(source)
    assert len(seeds) == len({item["title"] for item in seeds}) == 20
    assert len({item["category"] for item in seeds}) > 5
    assert len({item["task_complexity"] for item in seeds}) > 1
    source.write_text(
        json.dumps({"count": 2, "categories": ["text diffing and patch application"]})
    )
    assert {row["category"] for row in sample_inputs(source)} == {
        "text diffing and patch application"
    }
    source.write_text('{"categories":["unknown"]}')
    with pytest.raises(ValueError, match="Unknown"):
        sample_inputs(source)


def test_separate_authors_receive_prior_stage_evidence(tmp_path, monkeypatch):
    calls = []
    program = "\n".join(f"def test_case_{i}():\n    assert {i} == {i}\n" for i in range(5))

    def complete(model, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            content=json.dumps(
                {
                    "description": "A concrete required outcome. " * 8,
                    "truth": "Private initial data. " * 8,
                }
                if len(calls) == 1
                else {"code": program}
            )
        )

    monkeypatch.setattr(templates, "metered_complete", complete)
    design = recipe.design(
        {"category": "text processing", "request": "Prepare the described report."},
        model=None,
        ledger=None,
        receipt=tmp_path / "design.json",
        operation_id="design:fixture",
        resume=False,
    )
    assert [call["operation_id"] for call in calls] == [
        "design:fixture",
        "design:fixture:initial",
        "design:fixture:final",
    ]
    assert json.loads(calls[2]["user"])["initial_tests"] == program
    assert "before" in calls[1]["system"]
    assert "FINAL" in calls[2]["system"]
    assert design.initial_tests == design.final_tests == program
    assert design.core_capabilities == ["text processing"]
