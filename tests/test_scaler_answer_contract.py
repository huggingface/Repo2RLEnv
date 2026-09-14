from __future__ import annotations

import pytest

from repo2rlenv.pipelines.recipes.scaler.families import answer_contract, instruction_for
from repo2rlenv.pipelines.recipes.scaler.grade import typed_equal


def test_declared_rings_tolerance_accepts_valid_rounding_and_rejects_wrong_result():
    contract = answer_contract(
        {"output_type": "number", "answer_tolerance": {"absolute": "1e-9", "relative": "1e-9"}}
    )
    reference = r"\boxed{1632.34573381740718411859}"
    assert typed_equal(reference, r"\boxed{1632.345735}", contract)
    assert not typed_equal(reference, r"\boxed{1632.3458}", contract)
    assert not typed_equal(reference, "nan", contract)


def test_numeric_integer_outputs_remain_exact_without_public_tolerance():
    contract = {"output_type": "number"}
    assert typed_equal(r"\boxed{9007199254740993}", "9007199254740993.0", contract)
    assert not typed_equal(r"\boxed{9007199254740993}", "9007199254740992", contract)
    assert not typed_equal("0", "1e-12", contract)


def test_string_grading_preserves_leading_zeros():
    contract = {"output_type": "string"}
    assert typed_equal(r"\boxed{0000011000}", "0000011000", contract)
    assert not typed_equal(r"\boxed{0000011000}", "11000", contract)
    assert not typed_equal(r"\boxed{0000011000}", "0000011001", contract)


@pytest.mark.parametrize("wrong", ["[2, 1]", "[1]", "[[1], 2]", "[true, 2]", "[1, 2, 3]"])
def test_array_grading_rejects_changed_order_shape_and_type(wrong):
    assert not typed_equal(r"\boxed{[1, 2]}", wrong, {"output_type": "array"})


def test_array_grading_keeps_large_integer_precision():
    contract = {"output_type": "array"}
    assert typed_equal("[9007199254740993, 2]", "[9007199254740993, 2.0]", contract)
    assert not typed_equal("[9007199254740993, 2]", "[9007199254740992, 2]", contract)


def test_tolerance_is_public_and_explicit():
    family = {
        "name": "Rings",
        "logic_description": "Compute the energy.",
        "output_type": "number",
        "answer_tolerance": {"absolute": "1e-9", "relative": "1e-9"},
    }
    instruction = instruction_for(family, {"x": 1})
    assert "absolute error is at most 1E-9" in instruction
    assert "relative error is at most 1E-9" in instruction
    for tolerance in ({"relative": -1}, {"absolute": "NaN"}, {"relative": "Infinity"}):
        with pytest.raises(ValueError):
            answer_contract({**family, "answer_tolerance": tolerance})
