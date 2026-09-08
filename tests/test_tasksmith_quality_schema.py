from __future__ import annotations

import asyncio
import copy
import json

import pytest
from pydantic import ValidationError

from repo2rlenv.tasksmith import inline_review
from repo2rlenv.tasksmith.models import QUALITY_DIMENSIONS, QualityReport
from tests.test_tasksmith_inline_review import inputs as _inline_inputs
from tests.test_tasksmith_inline_review import response, transport

EXPECTED_NAMES = {
    "useful_scope",
    "instruction_sufficiency",
    "verifier_correctness_coverage",
    "valid_alternative_tolerance",
    "oracle_validity",
    "isolation_leakage",
    "reproducibility_materialization",
    "trajectory_findings",
}


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_quality_schema_requires_exact_public_dimension_names_and_resolvable_criteria(mode):
    schema = QualityReport.model_json_schema(mode=mode)
    criteria = schema["properties"]["criteria"]
    assert criteria["type"] == "object"
    assert set(criteria["properties"]) == set(criteria["required"]) == EXPECTED_NAMES
    assert criteria["additionalProperties"] is False
    assert len(criteria["required"]) == len(EXPECTED_NAMES) == 8
    for value in criteria["properties"].values():
        assert value == {"$ref": "#/$defs/QualityCriterion"}
        assert schema["$defs"]["QualityCriterion"]["type"] == "object"
        assert {"status", "score", "explanation"} <= set(
            schema["$defs"]["QualityCriterion"]["required"]
        )


def test_quality_schema_preserves_custom_reference_templates():
    schema = QualityReport.model_json_schema(ref_template="#/components/schemas/{model}")
    assert all(
        item == {"$ref": "#/components/schemas/QualityCriterion"}
        for item in schema["properties"]["criteria"]["properties"].values()
    )


@pytest.mark.parametrize("change", ["missing", "extra", "renamed"])
def test_python_validation_still_rejects_wrong_names_after_schema_fix(tmp_path, change):
    inputs = _inline_inputs.__wrapped__(tmp_path)
    payload = json.loads(
        response(inputs).model_dump()["choices"][0]["message"]["tool_calls"][0]["function"][
            "arguments"
        ]
    )
    criterion = copy.deepcopy(payload["criteria"]["useful_scope"])
    if change in {"missing", "renamed"}:
        payload["criteria"].pop("useful_scope")
    if change in {"extra", "renamed"}:
        payload["criteria"]["instruction_following"] = criterion
    with pytest.raises(ValidationError, match="exactly the eight policy dimensions"):
        QualityReport.model_validate(payload)


def test_real_budget_transport_delivers_the_eight_named_properties_to_the_model(
    tmp_path, monkeypatch
):
    inputs = _inline_inputs.__wrapped__(tmp_path)
    result = response(inputs)

    def inspect(call):
        assert len(call["tools"]) == 1
        function = call["tools"][0]["function"]
        assert function["name"] == "emit_quality_report"
        schema = function["parameters"]
        criteria = schema["properties"]["criteria"]
        assert set(criteria["properties"]) == EXPECTED_NAMES
        assert set(criteria["required"]) == EXPECTED_NAMES
        assert criteria["additionalProperties"] is False
        assert set(QUALITY_DIMENSIONS) == EXPECTED_NAMES
        # Model output follows the schema it actually received, rather than a
        # test helper silently supplying names absent from the provider request.
        raw = result.model_dump()
        arguments = raw["choices"][0]["message"]["tool_calls"][0]["function"]
        payload = json.loads(arguments["arguments"])
        exemplar = next(iter(payload["criteria"].values()))
        payload["criteria"] = {name: copy.deepcopy(exemplar) for name in criteria["required"]}
        arguments["arguments"] = json.dumps(payload)

    calls = transport(monkeypatch, result, inspect)
    report = asyncio.run(inline_review.final_review_inline(**inputs))
    assert set(report.criteria) == EXPECTED_NAMES
    assert len(calls) == 1
    assert inputs["budget"].spent == pytest.approx(0.11)
    retained = json.loads((inputs["root"] / "inputs.json").read_text())
    assert retained["tools"] == calls[0]["tools"]
