from __future__ import annotations

import copy
import hashlib
import json

import pytest

from repo2rlenv.tasksmith.evidence_projection import project_dossier, restore_dossier


def test_exact_roundtrip_preserves_commands_reasoning_rewards_and_formatting():
    reasoning = "Visible reasoning and Unicode: π 🙂.\n" * 30
    texts = {
        "instruction": "Observe the public behavior.\n",
        "solver_0": "Actions\n"
        + json.dumps({"kind": "input", "prompt": reasoning}, indent=2)
        + "\n"
        + json.dumps(
            {
                "kind": "model",
                "reasoning_content": reasoning,
                "thinking_blocks": [{"thinking": reasoning}],
                "tool_calls": [{"command": "printf '%s' '<not executed>'"}],
            },
            ensure_ascii=True,
            indent=4,
        )
        + "\n"
        + json.dumps(
            {
                "kind": "tool",
                "output": {
                    "stdout": "Unique tool observation",
                    "stderr": "Unique error",
                    "exit_code": 7,
                },
            },
            indent=2,
        ),
        "controls": json.dumps(
            {"reward": None, "error": "cleanup unconfirmed", "cleanup_confirmed": False}
        ),
    }
    projected, receipt = project_dossier(texts)
    assert restore_dossier(projected, receipt) == texts
    assert set(projected) == set(texts) | {"shared_texts"}
    assert sum(map(len, projected.values())) < sum(map(len, texts.values()))
    assert projected["shared_texts"].count(reasoning) == 1
    assert "Unique tool observation" in projected["solver_0"]
    assert "cleanup unconfirmed" in projected["controls"]
    assert (
        receipt["roles"]["solver_0"]["original_sha256"]
        == hashlib.sha256(texts["solver_0"].encode()).hexdigest()
    )


def test_source_submissions_use_reconstructable_line_edits_only_if_smaller():
    source = "".join(f"line {n} holds distinct source text\n" for n in range(100))
    changed = source.replace("line 70 holds", "line 70 changes")
    texts = {
        "submissions": json.dumps(
            [{"trial": "oracle", "source": source}, {"trial": "solver", "source": changed}]
        )
    }
    projected, receipt = project_dossier(texts)
    assert restore_dossier(projected, receipt) == texts
    patches = [entry for entry in receipt["shared"].values() if entry["kind"] == "line_edits"]
    assert len(patches) == 1
    patch = patches[0]
    assert patch["end"] - patch["start"] < len(changed)
    assert "line 70 changes" in projected["shared_texts"]


def test_unrelated_source_is_not_encoded_as_larger_diff():
    texts = {"submissions": json.dumps({"left": "a" * 300 + "\n", "right": "b" * 300 + "\n"})}
    projected, receipt = project_dossier(texts)
    assert all(entry["kind"] == "literal" for entry in receipt["shared"].values())
    assert restore_dossier(projected, receipt) == texts


@pytest.mark.parametrize(
    "text",
    [
        '{"duplicate":1,"duplicate":2}\n',
        '{"number":NaN}\n',
        "plain @t0 text\n",
        '{"truncated":',
        '{ "float": 1.000e+03, "string": "\\u03c0" }\n',
    ],
)
def test_unknown_or_unusual_text_survives_exactly(text):
    projected, receipt = project_dossier({"role": text})
    assert restore_dossier(projected, receipt) == {"role": text}


@pytest.mark.parametrize("change", ["text", "pool", "missing_role", "original_hash", "format"])
def test_changed_projection_or_reconstruction_metadata_is_rejected(change):
    texts = {"role": json.dumps({"content": "visible content\n" * 100}, indent=2)}
    projected, receipt = project_dossier(texts)
    projected, receipt = dict(projected), copy.deepcopy(receipt)
    if change == "text":
        projected["role"] += "injected"
    elif change == "pool":
        projected["shared_texts"] += "injected"
    elif change == "missing_role":
        del projected["role"]
    elif change == "original_hash":
        receipt["roles"]["role"]["original_sha256"] = "0" * 64
    else:
        receipt["roles"]["role"]["documents"][0]["gaps"][0] = "hidden command"
    with pytest.raises(ValueError):
        restore_dossier(projected, receipt)


def test_exact_duplicate_plain_logs_have_single_required_readable_entry():
    logs = "Provider cleanup confirmed; exact unique resource context.\n" * 20
    texts = {"oracle": logs, "controls": logs, "adversary": logs}
    projected, receipt = project_dossier(texts)
    assert projected["oracle"] == projected["controls"] == projected["adversary"]
    assert len(receipt["shared"]) == 1
    assert restore_dossier(projected, receipt) == texts


def test_projection_does_not_fabricate_read_coverage_or_drop_roles():
    texts = {f"role_{n}": f"Unique content {n}" for n in range(14)}
    projected, receipt = project_dossier(texts)
    assert set(receipt["roles"]) == set(texts)
    assert not {"complete", "coverage", "read_spans"} & set(receipt)
    assert restore_dossier(projected, receipt) == texts


def test_nested_json_tool_outputs_deduplicate_source_without_losing_escaping():
    source = "".join(f"line {n} has complete source content and Unicode π\n" for n in range(90))
    output0 = json.dumps({"stdout": source, "stderr": "", "exit_code": 0}, ensure_ascii=True)
    output1 = json.dumps(
        {
            "stdout": source.replace("line 80 has", "line 80 changed"),
            "stderr": "unique warning",
            "exit_code": 0,
        },
        ensure_ascii=True,
    )
    texts = {
        "solver_0": json.dumps({"output": output0}, indent=2),
        "solver_1": json.dumps({"output": output1}, indent=4),
    }
    projected, receipt = project_dossier(texts)
    assert any(entry["kind"] == "nested_json" for entry in receipt["shared"].values())
    assert any(entry["kind"] == "line_edits" for entry in receipt["shared"].values())
    assert restore_dossier(projected, receipt) == texts
    assert "unique warning" in projected["shared_texts"]
    assert sum(map(len, projected.values())) < sum(map(len, texts.values()))


def test_deep_nested_json_preserves_every_layer_at_bounded_recursion_depth():
    value = "unique observation\n" * 30
    for _ in range(8):
        value = json.dumps({"payload": value})
    original = {"adversary": value}
    projected, receipt = project_dossier(original)
    assert restore_dossier(projected, receipt) == original
    assert sum(entry["kind"] == "nested_json" for entry in receipt["shared"].values()) <= 4


def test_canonical_receipt_serialization_and_input_order_preserve_roundtrip():
    originals = {f"role_{n}": json.dumps({"data": f"distinct item {n}\n" * 50}) for n in range(15)}
    projected, receipt = project_dossier(originals)
    serialized = json.loads(json.dumps(receipt, sort_keys=True))
    assert restore_dossier(projected, serialized) == originals
    reordered, second_receipt = project_dossier(dict(reversed(list(originals.items()))))
    assert reordered == projected
    assert second_receipt == receipt
