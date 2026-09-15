from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from repo2rlenv.campaigns.structured import validated_complete
from repo2rlenv.pipelines.recipes.terminal.templates import TestProgram as GeneratedTestProgram


def test_invalid_completed_test_program_is_corrected_without_repeating_other_stages(
    monkeypatch, tmp_path
):
    calls = []
    valid = "\n".join(f"def test_{n}():\n    assert {n} == {n}\n" for n in range(5))

    def complete(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            content=json.dumps(
                {"code": "def test_one():\ntry:\n" * 8 if len(calls) == 1 else valid}
            )
        )

    monkeypatch.setattr("repo2rlenv.campaigns.structured.metered_complete", complete)
    result = validated_complete(
        GeneratedTestProgram,
        None,
        ledger=None,
        receipt=tmp_path / "initial-model.json",
        operation_id="design:one:initial",
        system="Generate tests",
        payload={"task": "owned fixture"},
        reservation_usd="0.75",
        max_tokens=1000,
        resume=False,
    )
    assert result.code == valid
    assert [call["operation_id"] for call in calls] == [
        "design:one:initial",
        "design:one:initial:repair:1",
    ]
    assert "validation_error" in json.loads(calls[1]["user"])
    assert (tmp_path / "initial-model-validation.json").exists()


def test_provider_timeout_does_not_trigger_a_new_paid_request(monkeypatch, tmp_path):
    calls = []

    def complete(*args, **kwargs):
        calls.append(kwargs)
        raise TimeoutError("Uncertain provider outcome")

    monkeypatch.setattr("repo2rlenv.campaigns.structured.metered_complete", complete)
    with pytest.raises(TimeoutError):
        validated_complete(
            GeneratedTestProgram,
            None,
            ledger=None,
            receipt=tmp_path / "model.json",
            operation_id="owned",
            system="test",
            payload={},
            reservation_usd="1",
            max_tokens=1000,
            resume=False,
        )
    assert len(calls) == 1


def test_schema_repairs_stop_at_the_configured_limit(monkeypatch, tmp_path):
    calls = []

    def complete(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content=json.dumps({"code": "# no executable tests\n" * 8}))

    monkeypatch.setattr("repo2rlenv.campaigns.structured.metered_complete", complete)
    with pytest.raises(ValueError, match="exhausted 3 attempts"):
        validated_complete(
            GeneratedTestProgram,
            None,
            ledger=None,
            receipt=tmp_path / "model.json",
            operation_id="owned",
            system="test",
            payload={},
            reservation_usd="1",
            max_tokens=1000,
            resume=False,
        )
    assert len(calls) == 3
