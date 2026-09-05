from __future__ import annotations

import json

import pytest

from repo2rlenv.curation.models import VerifierPreflightReview, VerifierReview
from repo2rlenv.curation.verifier_review import _check_authority_inventory
from tests.test_curation_verifier_review import (
    feedback,
    read_all,
    record_path,
    review,
    setup,
    state,
    verifier,
)

# Imported fixture supplies a temporary task and mocks only the reviewer runtime.
__all__ = ["setup"]


def check(result="distinguished", requirement="compiled"):
    return {
        "requirement_id": requirement,
        "authoritative_input": "checkpoint module selection",
        "competing_input": "config module selection",
        "public_condition": "target is compiled",
        "discordant_fixture": "checkpoint selects left; config selects right",
        "expected_observation": "only left has adapter keys",
        "conditional_shortcut": "ignore checkpoint and use config only when compiled",
        "distinguishing_test": "test_compiled" if result == "distinguished" else None,
        "result": result,
        "reason": "The independently expected left keys differ from the right keys produced by config substitution.",
        "evidence": [
            {
                "path": "instruction.md",
                "line": 1,
                "quote": "Checkpoint selection takes precedence, including compiled targets.",
            },
            {"path": "tests/test_contract.py", "line": 2, "quote": "assert actual == ['left']"},
        ],
    }


def report(rows):
    return VerifierPreflightReview.model_validate(
        {
            "score": 4,
            "blockers": [],
            "repairs": [],
            "evidence": ["Observed requirement and protected fixture/assertion mappings."],
            "authority_checks": rows,
        }
    )


def texts():
    return {
        "instruction.md": "Checkpoint selection takes precedence, including compiled targets.",
        "contract.json": json.dumps(
            {"requirements": [{"id": "compiled", "tests": ["test_compiled"]}]}
        ),
        "tests/test_contract.py": "def verify(actual):\n    assert actual == ['left']\n\ndef test_compiled():\n    verify(probe())\n",
    }


def test_legacy_records_parse_but_current_schema_requires_worksheet():
    legacy = {"score": 4, "blockers": [], "repairs": [], "evidence": ["Earlier static evidence"]}
    assert VerifierReview.model_validate(legacy).passed
    with pytest.raises(ValueError, match="authority_checks"):
        VerifierPreflightReview.model_validate(legacy)
    with pytest.raises(ValueError, match="authority_checks"):
        VerifierPreflightReview.model_validate({**legacy, "authority_checks": []})
    assert "authority_checks" in VerifierPreflightReview.model_json_schema()["required"]


def test_gap_produces_required_author_feedback_even_when_judge_claims_pass():
    row = check("gap")
    result = report([row])
    assert result.score == 2 and not result.passed
    assert "checkpoint selects left; config selects right" in result.repairs[0]
    assert "only when compiled" in result.repairs[0]
    # Normalization is idempotent for state/cache validation, not a new model call.
    assert VerifierPreflightReview.model_validate_json(result.model_dump_json()) == result


def test_linked_helper_assertion_is_accepted():
    _check_authority_inventory(report([check()]), texts())


def test_plain_path_assertion_cannot_support_compiled_challenge():
    material = texts()
    material["tests/test_contract.py"] = (
        "def test_plain():\n    assert actual == ['left']\n\ndef test_compiled():\n    probe()\n"
    )
    with pytest.raises(ValueError, match="reachable"):
        _check_authority_inventory(report([check()]), material)
    wrong_mapping = check()
    wrong_mapping["distinguishing_test"] = "test_plain"
    with pytest.raises(ValueError, match="map to compiled"):
        _check_authority_inventory(report([wrong_mapping]), material)
    # Reporting the missing compiled observation as a gap is valid repair feedback.
    gap = report([check("gap")])
    _check_authority_inventory(gap, material)
    assert not gap.passed


@pytest.mark.parametrize(
    "body",
    [
        "def test_compiled(verify):\n    pass\n",
        "@pytest.mark.usefixtures('verify')\ndef test_compiled():\n    pass\n",
    ],
)
def test_pytest_fixture_assertions_have_static_linkage(body):
    material = texts()
    material["tests/test_contract.py"] = "def verify():\n    assert actual == ['left']\n\n" + body
    _check_authority_inventory(report([check()]), material)


def test_pytest_exception_assertion_is_not_rejected_for_lacking_assert_keyword():
    material = texts()
    material["tests/test_contract.py"] = (
        "def test_compiled():\n    with pytest.raises(ValueError):\n        probe()\n"
    )
    row = check()
    row["evidence"][1]["quote"] = "with pytest.raises(ValueError):"
    _check_authority_inventory(report([row]), material)


@pytest.mark.parametrize(
    "change",
    [
        "unknown_requirement",
        "missing_requirement",
        "unknown_file",
        "wrong_quote",
        "wrong_line",
        "worker_assertion",
        "comment",
        "missing_public_citation",
    ],
)
def test_invalid_inventory_or_evidence_fails_closed(change):
    material, row = texts(), check()
    if change == "unknown_requirement":
        row["requirement_id"] = "invented"
    elif change == "missing_requirement":
        contract = json.loads(material["contract.json"])
        contract["requirements"].append({"id": "plain", "tests": ["test_plain"]})
        material["contract.json"] = json.dumps(contract)
    elif change == "unknown_file":
        row["evidence"][1]["path"] = "../outside.py"
    elif change == "wrong_quote":
        row["evidence"][1]["quote"] = "assert actual == ['right']"
    elif change == "wrong_line":
        row["evidence"][1]["line"] = 4
    elif change == "worker_assertion":
        material["tests/test_contract.py"] = (
            "CODE = '''\nassert actual == ['left']\n'''\ndef test_compiled():\n    run_probe(CODE)\n"
        )
    elif change == "comment":
        material["tests/test_contract.py"] = (
            "def test_compiled():\n    # assert actual == ['left']\n    probe()\n"
        )
    else:
        row["evidence"] = row["evidence"][1:]
    with pytest.raises(ValueError):
        _check_authority_inventory(report([row]), material)


def test_grounded_na_covers_requirement_without_inventing_authority():
    row = feedback().authority_checks[0]
    material = {
        "instruction.md": row.evidence[0].quote,
        "contract.json": '{"requirements":[{"id":"sum","tests":["test_sum"]}]}',
    }
    _check_authority_inventory(feedback(), material)
    invalid = row.model_dump()
    invalid["reason"] = "N/A"
    with pytest.raises(ValueError, match="reason"):
        report([invalid])


@pytest.mark.asyncio
async def test_new_policy_rejects_old_final_then_accepts_corrected_worksheet_with_same_caps(setup):
    s = setup
    seen = []

    async def judge(**kwargs):
        await read_all(kwargs)
        legacy = feedback().model_dump()
        legacy.pop("authority_checks")
        assert "input-authority worksheet" in kwargs["validate_final"](json.dumps(legacy))
        seen.append(legacy)
        assert kwargs["validate_final"](feedback().model_dump_json()) is None
        assert kwargs["max_turns"] == 10 and kwargs["max_cost"] == 4
        return state()

    s.agent.side_effect = judge
    assert (await review(s)).passed
    assert seen and s.agent.await_count == 1


@pytest.mark.asyncio
async def test_cached_new_policy_pass_missing_worksheet_fails_without_paid_retry(setup):
    s = setup
    await review(s)
    path = record_path(s)
    record = json.loads(path.read_text())
    assert record["identity"]["policy_version"] == 10
    record["review"].pop("authority_checks")
    path.write_text(json.dumps(record))
    with pytest.raises(verifier.VerifierReviewError, match="Cached verifier review unavailable"):
        await review(s)
    s.agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconsidered_final_still_gets_missing_worksheet_feedback(setup):
    s = setup
    initial = feedback().model_copy(
        update={"optional_improvements": ["A harmless naming cleanup."]}
    )

    async def judge(**kwargs):
        await read_all(kwargs)
        validate = kwargs["validate_final"]
        assert validate(initial.model_dump_json()) == verifier.RECONSIDER_PASS
        missing = initial.model_dump()
        missing.pop("authority_checks")
        assert "input-authority worksheet" in validate(json.dumps(missing))
        assert validate(initial.model_dump_json()) is None
        result = state(initial)
        result["messages"][:0] = [
            {"role": "assistant", "content": initial.model_dump_json()},
            {"role": "user", "content": verifier.RECONSIDER_PASS},
        ]
        return result

    s.agent.side_effect = judge
    assert (await review(s)).passed


@pytest.mark.asyncio
async def test_gap_feedback_cached_with_original_model_text_preserved(setup):
    s = setup
    raw = feedback().model_dump()
    row = raw["authority_checks"][0]
    row.update(
        result="gap",
        authoritative_input="explicit sequence",
        competing_input="cached sequence",
        discordant_fixture="fresh sequence differs from cached sequence",
        expected_observation="sum of the fresh sequence",
        conditional_shortcut="return cached sum when cache exists",
    )

    async def judge(**kwargs):
        await read_all(kwargs)
        return state(content=json.dumps(raw))

    s.agent.side_effect = judge
    result = await review(s)
    assert not result.passed and result.score == 2 and result.repairs
    stored = json.loads(record_path(s).with_name("state.json").read_text())
    assert json.loads(stored["messages"][-1]["content"])["score"] == 4
    assert (await review(s)) == result
    s.agent.assert_awaited_once()


def test_module_local_helper_does_not_borrow_unrelated_same_named_function():
    material = texts()
    material["tests/test_contract.py"] = (
        "def verify(actual):\n    return actual\n\ndef test_compiled():\n    verify(probe())\n"
    )
    material["tests/unrelated.py"] = "def verify(actual):\n    assert actual == ['left']\n"
    row = check()
    row["evidence"][1]["path"] = "tests/unrelated.py"
    with pytest.raises(ValueError, match="reachable"):
        _check_authority_inventory(report([row]), material)


def test_explicit_imported_helper_reference_is_resolved():
    material = texts()
    material["tests/test_contract.py"] = (
        "from .helpers import verify as compare\ndef test_compiled():\n    compare(probe())\n"
    )
    material["tests/helpers.py"] = "def verify(actual):\n    assert actual == ['left']\n"
    row = check()
    row["evidence"][1]["path"] = "tests/helpers.py"
    _check_authority_inventory(report([row]), material)


@pytest.mark.parametrize(
    "expression",
    [
        "np.testing.assert_allclose(actual, expected)",
        "torch.testing.assert_close(actual, expected)",
        "custom_numerical_assert(actual, expected)",
        "get_checker()(actual, expected)",
    ],
)
def test_numerical_custom_or_dynamic_assertion_calls_remain_valid_references(expression):
    material = texts()
    material["tests/test_contract.py"] = "def test_compiled():\n    " + expression + "\n"
    row = check()
    row["evidence"][1]["quote"] = expression
    _check_authority_inventory(report([row]), material)


def test_unittest_method_reference_is_supported():
    material = texts()
    material["tests/test_contract.py"] = (
        "class TestContract(unittest.TestCase):\n    def test_compiled(self):\n        self.assertEqual(actual, expected)\n"
    )
    row = check()
    row["evidence"][1].update(line=3, quote="self.assertEqual(actual, expected)")
    _check_authority_inventory(report([row]), material)


def test_omitted_lines_are_resolved_from_unique_raw_quotes():
    row = check()
    for evidence in row["evidence"]:
        evidence.pop("line")
    result = report([row])
    _check_authority_inventory(result, texts())
    assert [e.line for e in result.authority_checks[0].evidence] == [1, 2]


def test_repeated_test_quote_selects_mapped_helper_occurrence():
    row = check()
    row["evidence"][1].pop("line")
    material = texts()
    material["tests/test_contract.py"] += "\ndef unrelated():\n    assert actual == ['left']\n"
    result = report([row])
    _check_authority_inventory(result, material)
    assert result.authority_checks[0].evidence[1].line == 2
    row["evidence"][1]["line"] = 2
    _check_authority_inventory(report([row]), material)


@pytest.mark.asyncio
async def test_host_resolved_lines_cache_without_changing_raw_model_output(setup):
    s = setup
    raw = feedback().model_dump()
    raw["authority_checks"][0]["evidence"][0].pop("line")

    async def judge(**kwargs):
        await read_all(kwargs)
        assert kwargs["validate_final"](json.dumps(raw)) is None
        return state(content=json.dumps(raw))

    s.agent.side_effect = judge
    result = await review(s)
    assert result.authority_checks[0].evidence[0].line == 1
    assert (await review(s)) == result
    saved = json.loads(record_path(s).with_name("state.json").read_text())
    original = json.loads(saved["messages"][-1]["content"])
    assert "line" not in original["authority_checks"][0]["evidence"][0]
    s.agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_optional_line_reconsideration_normalizes_raw_initial_and_reuses_cache(setup):
    s = setup
    raw = feedback().model_dump()
    raw["optional_improvements"] = ["An optional naming cleanup only."]
    raw["authority_checks"][0]["evidence"][0].pop("line")

    async def judge(**kwargs):
        await read_all(kwargs)
        validate = kwargs["validate_final"]
        original_text = json.dumps(raw)
        assert validate(original_text) == verifier.RECONSIDER_PASS
        assert validate(original_text) is None
        result = state(content=original_text)
        result["messages"][:0] = [
            {"role": "assistant", "content": original_text},
            {"role": "user", "content": verifier.RECONSIDER_PASS},
        ]
        return result

    s.agent.side_effect = judge
    first = await review(s)
    assert first.passed and first.authority_checks[0].evidence[0].line == 1
    assert await review(s) == first
    stored = json.loads(record_path(s).with_name("state.json").read_text())
    assert (
        "line"
        not in json.loads(stored["messages"][0]["content"])["authority_checks"][0]["evidence"][0]
    )
    s.agent.assert_awaited_once()


def test_repeated_public_contract_quote_resolves_first_occurrence():
    material = texts()
    material["instruction.md"] += "\nRepeated requirement:\n" + material["instruction.md"]
    row = check()
    row["evidence"][0].pop("line")
    result = report([row])
    _check_authority_inventory(result, material)
    assert result.authority_checks[0].evidence[0].line == 1


def test_repeated_assertion_chooses_later_mapped_helper_not_first_unrelated_hit():
    material = texts()
    material["tests/test_contract.py"] = (
        "def unrelated():\n    assert actual == ['left']\n\n" + material["tests/test_contract.py"]
    )
    row = check()
    row["evidence"][1].pop("line")
    result = report([row])
    _check_authority_inventory(result, material)
    assert result.authority_checks[0].evidence[1].line == 5


def test_missing_link_error_reports_requirement_condition_and_hit_lines():
    material = texts()
    material["tests/test_contract.py"] = (
        "def unrelated():\n    assert actual == ['left']\n\ndef test_compiled():\n    probe()\n"
    )
    row = check()
    row["evidence"][1].pop("line")
    with pytest.raises(ValueError) as error:
        _check_authority_inventory(report([row]), material)
    message = str(error.value)
    assert "[compiled] under target is compiled" in message
    assert "candidate hit lines: [2]" in message
    assert "eligible lines: [5]" in message


def test_wrong_explicit_line_still_rejected_with_correct_candidate_line():
    row = check()
    row["evidence"][1]["line"] = 3
    with pytest.raises(ValueError, match=r"does not match.*:3; candidate hit lines: \[2\]"):
        _check_authority_inventory(report([row]), texts())


@pytest.mark.parametrize(
    "joined",
    ["test_one,test_two", "test_one / test_two", "test_one and test_two", "[test_one, test_two]"],
)
def test_distinguishing_test_requires_exactly_one_machine_name(joined):
    row = check()
    row["distinguishing_test"] = joined
    with pytest.raises(ValueError, match="distinguishing_test"):
        report([row])
    schema = VerifierPreflightReview.model_json_schema()["$defs"]["InputAuthorityCheck"][
        "properties"
    ]["distinguishing_test"]
    assert any(part.get("pattern") == r"^test_[A-Za-z0-9_]+$" for part in schema["anyOf"])


def test_reference_errors_are_batched_across_rows_and_evidence():
    material = texts()
    first, second = check(), check()
    first["evidence"] = first["evidence"][1:]  # Missing public citation.
    first["evidence"][0]["line"] = 8
    second["public_condition"] = "source is compiled"
    second["evidence"][0]["quote"] = "invented source text"
    with pytest.raises(ValueError) as error:
        _check_authority_inventory(report([first, second]), material)
    message = str(error.value)
    assert "authority_checks[0] [compiled] under target is compiled" in message
    assert "public-contract citation" in message
    assert "candidate hit lines: [2]" in message
    assert "authority_checks[1] [compiled] under source is compiled" in message
    assert "candidate hit lines: []" in message


@pytest.mark.parametrize("line", [None, 3])
def test_supplementary_fixture_quote_does_not_need_to_be_an_assertion(line):
    material = texts()
    material["tests/test_contract.py"] = (
        "def test_compiled():\n    explicit = ['left']\n    cached = ['right']\n"
        "    actual = probe(explicit, cached)\n    assert actual == ['left']\n"
    )
    row = check()
    row["evidence"][1].update(line=None)
    row["evidence"].append(
        {"path": "tests/test_contract.py", "quote": "cached = ['right']", "line": line}
    )
    result = report([row])
    _check_authority_inventory(result, material)
    assert result.authority_checks[0].evidence[-1].line == 3
    assert result.authority_checks[0].evidence[1].line == 5


@pytest.mark.parametrize(
    "decoration,test_signature",
    [
        ("@pytest.fixture(name='validated')", "validated"),
        ("@pytest.fixture(autouse=True)", ""),
        ("@fixture(name='validated')", "validated"),
    ],
)
def test_module_fixture_alias_and_autouse_assertions_are_mapped(decoration, test_signature):
    material = texts()
    material["tests/test_contract.py"] = (
        "import pytest\nfrom pytest import fixture\n" + decoration + "\n"
        "def verify():\n    actual = probe()\n    assert actual == ['left']\n\n"
        f"def test_compiled({test_signature}):\n    pass\n"
    )
    row = check()
    row["evidence"][1]["line"] = None
    result = report([row])
    _check_authority_inventory(result, material)
    assert result.authority_checks[0].evidence[1].line == 6


def test_autouse_fixture_follows_its_explicit_fixture_dependencies():
    material = texts()
    material["tests/test_contract.py"] = (
        "import pytest\n@pytest.fixture(name='validated')\ndef verify():\n"
        "    assert actual == ['left']\n\n@pytest.fixture(autouse=True)\n"
        "def activate(validated):\n    pass\n\ndef test_compiled():\n    pass\n"
    )
    row = check()
    row["evidence"][1]["line"] = None
    _check_authority_inventory(report([row]), material)


@pytest.mark.parametrize(
    "decoration", ["@pytest.fixture(name='validated')", "@pytest.fixture(autouse=True)"]
)
def test_fixture_in_unrelated_module_cannot_ground_mapped_test(decoration):
    material = texts()
    material["tests/test_contract.py"] = "def test_compiled():\n    pass\n"
    material["tests/unrelated.py"] = (
        "import pytest\n" + decoration + "\ndef verify():\n    assert actual == ['left']\n"
    )
    row = check()
    row["evidence"][1].update(path="tests/unrelated.py", line=None)
    with pytest.raises(ValueError, match="reachable"):
        _check_authority_inventory(report([row]), material)


def test_supplementary_quote_alone_cannot_replace_mapped_check():
    material = texts()
    material["tests/test_contract.py"] = "def test_compiled():\n    cached = ['right']\n"
    row = check()
    row["evidence"][1].update(quote="cached = ['right']", line=None)
    with pytest.raises(ValueError, match="must cite an assertion or call"):
        _check_authority_inventory(report([row]), material)
