from __future__ import annotations

import copy
import importlib
import itertools
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from repo2rlenv.curation.budget import Budget
from repo2rlenv.curation.models import (
    ConditionMatrix,
    Review,
    VerifierPreflightReview,
    VerifierPreflightReviewV11,
    VerifierReview,
)

verifier = importlib.import_module("repo2rlenv.curation.verifier_review")
PUBLIC = (
    "Source and target may independently be plain or compiled; ordinary and interior names "
    "must preserve every interior _orig_mod component in every wrapper combination."
)


def cite(quote, path="instruction.md", **kwargs):
    return {"path": path, "quote": quote, **kwargs}


def example():
    """Static source strings only: no repository imports or worker execution."""
    axes = [
        ("source", ["plain", "compiled"]),
        ("target", ["plain", "compiled"]),
        ("name", ["ordinary", "interior"]),
    ]
    cases, code = [], []
    for index, values in enumerate(itertools.product(*(values for _, values in axes))):
        test = f"test_joint_{index}"
        fixture = (
            f"actual = observe(source={values[0]!r}, target={values[1]!r}, name={values[2]!r})"
        )
        expected = "left._orig_mod.q_proj" if values[2] == "interior" else "left.q_proj"
        assertion = f"assert actual == {expected!r}"
        code.append(f"def {test}():\n    {fixture}\n    {assertion}\n")
        cases.append(
            {
                "values": dict(zip((name for name, _ in axes), values, strict=True)),
                "result": "covered",
                "fixture_input": fixture,
                "expected_observation": f"The fixed public name is {expected!r}.",
                "test": test,
                "fixture_evidence": [cite(fixture, "tests/test_contract.py")],
                "expected_evidence": [cite(assertion, "tests/test_contract.py")],
                "reason": "The same call sets all three categories and checks a fixed literal name.",
            }
        )
    matrix = {
        "requirement_ids": ["names"],
        "interaction_reason": "Both independently selected wrappers transform the same name, including interior components.",
        "evidence": [cite(PUBLIC)],
        "axes": [
            {
                "name": name,
                "values": values,
                "public_meaning": f"Publicly independent {name} categories in the name-preservation contract.",
                "evidence": [cite(PUBLIC)],
            }
            for name, values in axes
        ],
        "cases": cases,
    }
    record = {
        "score": 4,
        "blockers": [],
        "repairs": [],
        "evidence": ["Protected fixed name expectations cover the declared joint conditions."],
        "authority_checks": [
            {
                "requirement_id": "names",
                "authoritative_input": None,
                "competing_input": None,
                "public_condition": "Literal public name preservation",
                "discordant_fixture": None,
                "expected_observation": None,
                "conditional_shortcut": None,
                "distinguishing_test": None,
                "result": "not_applicable",
                "reason": "This synthetic inventory fixture checks name categories, without a competing configuration source.",
                "evidence": [cite(PUBLIC)],
            }
        ],
        "condition_matrices": [matrix],
    }
    texts = {
        "instruction.md": PUBLIC,
        "contract.json": json.dumps(
            {"requirements": [{"id": "names", "tests": [case["test"] for case in cases]}]}
        ),
        "tests/test_contract.py": "\n".join(code),
        "solution/solve.sh": "# GOLD and mutations are not joint coverage evidence.\n",
    }
    return record, texts


def check(record, texts):
    result = VerifierPreflightReviewV11.model_validate(record)
    verifier._check_preflight_inventories(result, texts)
    return result


def test_policy_11_is_required_only_for_current_preflight():
    record, texts = example()
    legacy = copy.deepcopy(record)
    del legacy["condition_matrices"]
    assert VerifierReview.model_validate(legacy).passed
    old = VerifierPreflightReview.model_validate(legacy)
    verifier._check_authority_inventory(old, texts)
    assert old.passed
    with pytest.raises(ValidationError, match="condition_matrices"):
        VerifierPreflightReviewV11.model_validate(legacy)
    assert "condition_matrices" not in Review.model_fields
    assert "condition_matrices" not in VerifierReview.model_fields
    assert "condition_matrices" not in VerifierPreflightReview.model_fields
    assert "condition_matrices" in VerifierPreflightReviewV11.model_json_schema()["required"]
    assert verifier.POLICY_VERSION == 11


def test_joint_inventory_resolves_repeated_assertions_to_the_named_test():
    record, texts = example()
    review = check(record, texts)
    assert review.passed
    cases = review.condition_matrices[0].cases
    assert len(cases) == 8
    assert cases[1].expected_evidence[0].line != cases[7].expected_evidence[0].line
    assert check(review.model_dump(), texts) == review


@pytest.mark.parametrize("replacement", [False, True])
def test_all_marginals_cannot_hide_missing_both_compiled_interior_case(replacement):
    record, _ = example()
    cases = record["condition_matrices"][0]["cases"]
    missing = cases.pop()
    assert missing["values"] == {"source": "compiled", "target": "compiled", "name": "interior"}
    # All values and each pair still appear among the other seven observations.
    for pair in itertools.combinations(missing["values"], 2):
        assert {tuple(case["values"][axis] for axis in pair) for case in cases} == {
            tuple(case["values"][axis] for axis in pair) for case in [*cases, missing]
        }
    if replacement:
        cases.append(copy.deepcopy(cases[0]))
    with pytest.raises(ValidationError, match=r"Duplicate joint|omits joint combinations"):
        VerifierPreflightReviewV11.model_validate(record)


def test_missing_joint_observation_becomes_required_repair_without_claiming_execution():
    record, texts = example()
    case = record["condition_matrices"][0]["cases"][-1]
    case.update(
        result="gap",
        test=None,
        fixture_evidence=[],
        expected_evidence=[],
        reason="The existing observations never combine both wrappers with an interior component; proposed fixture feasibility is unverified.",
    )
    review = check(record, texts)
    assert review.score == 2 and not review.passed
    assert len(review.repairs) == 1
    assert "source=compiled" in review.repairs[0]
    assert "target=compiled" in review.repairs[0]
    assert "name=interior" in review.repairs[0]
    assert "unverified" in review.repairs[0]
    assert check(review.model_dump(), texts).repairs == review.repairs


def test_no_interaction_is_explicit_and_requires_a_real_public_citation():
    record, texts = example()
    simple_public = "Return the supplied integer unchanged."
    texts["instruction.md"] = simple_public
    texts["contract.json"] = '{"requirements": [{"id": "names", "tests": ["test_identity"]}]}'
    texts["tests/test_contract.py"] = "def test_identity():\n    assert identity(7) == 7\n"
    record["authority_checks"][0]["evidence"] = [cite(simple_public)]
    matrix = record["condition_matrices"][0]
    matrix.update(
        axes=[],
        cases=[],
        evidence=[cite(simple_public)],
        interaction_reason="The sole scalar identity promise declares no independently varying options whose joint settings affect it.",
    )
    assert check(record, texts).passed
    matrix["evidence"][0]["quote"] = "This unsupported scope claim is not in any source."
    with pytest.raises(ValueError, match="exact occurrence"):
        check(record, texts)


def test_all_requirement_ids_need_an_interaction_assessment():
    record, texts = example()
    texts["contract.json"] = json.dumps(
        {"requirements": [{"id": "names", "tests": ["test_joint_0"]}, {"id": "other", "tests": []}]}
    )
    with pytest.raises(ValueError, match="assess every requirement"):
        verifier._check_condition_inventory(
            VerifierPreflightReviewV11.model_validate(record), texts
        )


@pytest.mark.parametrize("field", ["fixture_evidence", "expected_evidence"])
def test_covered_case_requires_both_fixture_and_expected_observation(field):
    record, _ = example()
    record["condition_matrices"][0]["cases"][0][field] = []
    with pytest.raises(ValidationError, match="fixture evidence and expected-observation evidence"):
        VerifierPreflightReviewV11.model_validate(record)


@pytest.mark.parametrize("field", ["fixture_evidence", "expected_evidence"])
def test_gold_or_mutation_cannot_stand_in_for_protected_case_evidence(field):
    record, texts = example()
    record["condition_matrices"][0]["cases"][0][field] = [
        cite("GOLD and mutations", "solution/solve.sh")
    ]
    with pytest.raises(ValueError, match="protected tests, not GOLD or mutation"):
        check(record, texts)


def test_expected_observation_must_reach_a_protected_assertion_not_worker_self_assert():
    record, texts = example()
    texts["tests/test_contract.py"] += '\nWORKER = "assert worker_value == 100"\n'
    record["condition_matrices"][0]["cases"][0]["expected_evidence"] = [
        cite("assert worker_value == 100", "tests/test_contract.py")
    ]
    with pytest.raises(ValueError, match="protected assertion reachable"):
        check(record, texts)


def test_unrelated_protected_assertion_does_not_prove_mapped_case():
    record, texts = example()
    texts["tests/test_contract.py"] += "\ndef test_unrelated():\n    assert answer == 42\n"
    case = record["condition_matrices"][0]["cases"][0]
    case["expected_evidence"] = [cite("assert answer == 42", "tests/test_contract.py")]
    with pytest.raises(ValueError, match="protected assertion reachable"):
        check(record, texts)
    case["test"] = "test_unrelated"
    with pytest.raises(ValueError, match="map to a declared requirement"):
        check(record, texts)


def test_same_name_roots_cannot_split_fixture_and_expected_observation():
    record, texts = example()
    case = record["condition_matrices"][0]["cases"][0]
    assertion = case["expected_evidence"][0]["quote"]
    texts["tests/test_contract.py"] = texts["tests/test_contract.py"].replace(
        "    " + assertion + "\n", "", 1
    )
    texts["tests/test_other.py"] = f"def {case['test']}():\n    {assertion}\n"
    case["expected_evidence"] = [cite(assertion, "tests/test_other.py")]
    with pytest.raises(ValueError, match="share one reachable protected-test root"):
        check(record, texts)


def test_same_name_roots_allow_a_coherent_later_root_without_pinning_quotes():
    record, texts = example()
    case = record["condition_matrices"][0]["cases"][0]
    fixture = case["fixture_evidence"][0]["quote"]
    assertion = case["expected_evidence"][0]["quote"]
    # Same module, distinct class roots: repeated fixture text needs fresh
    # omitted-line resolution after the first root lacks an expected assertion.
    texts["tests/test_contract.py"] = texts["tests/test_contract.py"].replace(
        f"def {case['test']}():\n    {fixture}\n    {assertion}\n",
        f"class TestFirst:\n    def {case['test']}(self):\n        {fixture}\n\n"
        f"class TestSecond:\n    def {case['test']}(self):\n        {fixture}\n        {assertion}\n",
        1,
    )
    result = check(record, texts)
    assert result.passed
    resolved = result.condition_matrices[0].cases[0]
    lines = texts["tests/test_contract.py"].splitlines()
    assert resolved.fixture_evidence[0].line == lines.index("        " + fixture, 4) + 1
    assert check(result.model_dump(), texts) == result


def test_paired_evidence_can_use_separate_helpers_reachable_from_same_root():
    record, texts = example()
    case = record["condition_matrices"][0]["cases"][0]
    fixture = case["fixture_evidence"][0]["quote"]
    assertion = case["expected_evidence"][0]["quote"]
    texts["tests/test_contract.py"] = (
        "from .inputs import make_actual\nfrom .checks import check_actual\n"
        + texts["tests/test_contract.py"].replace(
            f"    {fixture}\n    {assertion}\n",
            "    check_actual(make_actual())\n",
            1,
        )
    )
    input_line = fixture.replace("actual = ", "return ", 1)
    texts["tests/inputs.py"] = f"def make_actual():\n    {input_line}\n"
    texts["tests/checks.py"] = f"def check_actual(actual):\n    {assertion}\n"
    texts["tests/test_other.py"] = f"def {case['test']}():\n    pass\n"
    case["fixture_evidence"] = [cite(input_line, "tests/inputs.py")]
    case["expected_evidence"] = [cite(assertion, "tests/checks.py")]
    assert check(record, texts).passed


def test_fixture_citation_must_link_to_test_or_parameter_grid():
    record, texts = example()
    texts["tests/test_contract.py"] += "\nUNUSED = ('compiled', 'compiled', 'interior')\n"
    record["condition_matrices"][0]["cases"][-1]["fixture_evidence"] = [
        cite("UNUSED = ('compiled', 'compiled', 'interior')", "tests/test_contract.py")
    ]
    with pytest.raises(ValueError, match="inputs need a citation reachable"):
        check(record, texts)


def test_parameterized_cases_and_independent_helper_assertions_are_permitted():
    record, texts = example()
    cases = record["condition_matrices"][0]["cases"]
    grid = repr([tuple(case["values"].values()) for case in cases])
    texts["tests/test_contract.py"] = (
        "import pytest\nfrom .checks import check_name\n"
        f"@pytest.mark.parametrize('source,target,name', {grid})\n"
        "def test_joint(source, target, name):\n"
        "    check_name(observe(source, target, name), name)\n"
    )
    texts["tests/checks.py"] = (
        "def check_name(actual, name):\n"
        "    expected = 'left._orig_mod.q_proj' if name == 'interior' else 'left.q_proj'\n"
        "    assert actual == expected\n"
    )
    texts["contract.json"] = '{"requirements": [{"id": "names", "tests": ["test_joint"]}]}'
    for case in cases:
        case.update(
            test="test_joint",
            fixture_evidence=[cite(repr(tuple(case["values"].values())), "tests/test_contract.py")],
            expected_evidence=[
                cite("assert actual == expected", "tests/checks.py"),
                cite(
                    "expected = 'left._orig_mod.q_proj' if name == 'interior' else 'left.q_proj'",
                    "tests/checks.py",
                ),
            ],
        )
    assert check(record, texts).passed


def test_protected_exception_expectation_is_an_assertion():
    _, texts = example()
    texts["tests/test_contract.py"] += (
        "\nfrom pytest import raises as expect_error\n"
        "def test_error():\n"
        "    with expect_error(ValueError, match='invalid'):\n"
        "        observe(source='compiled', target='compiled', name='interior')\n"
    )
    lines, found = verifier._authority_reference_lines(texts, "test_error", assertions_only=True)
    line = (
        texts["tests/test_contract.py"]
        .splitlines()
        .index("    with expect_error(ValueError, match='invalid'):")
        + 1
    )
    assert found and line in lines["tests/test_contract.py"]


@pytest.mark.parametrize(
    "module, assertion", [("numpy.testing", "assert_equal"), ("torch.testing", "assert_close")]
)
def test_known_testing_assertion_aliases_keep_reference_linkage(module, assertion):
    texts = {
        "tests/test_numeric.py": f"from {module} import {assertion} as equal\ndef test_numeric():\n    equal(actual, expected)\n"
    }
    lines, found = verifier._authority_reference_lines(texts, "test_numeric", assertions_only=True)
    assert found and 3 in lines["tests/test_numeric.py"]


@pytest.mark.parametrize("module", ["numpy.testing", "untrusted_checks"])
def test_joint_expected_assertion_alias_requires_supported_testing_namespace(module):
    record, texts = example()
    lines = [f"from {module} import assert_equal as equal"]
    for case in record["condition_matrices"][0]["cases"]:
        fixture = case["fixture_evidence"][0]["quote"]
        assertion = (
            case["expected_evidence"][0]["quote"].replace("assert actual == ", "equal(actual, ")
            + ")"
        )
        lines.extend([f"def {case['test']}():", f"    {fixture}", f"    {assertion}", ""])
        case["expected_evidence"] = [cite(assertion, "tests/test_contract.py", line=len(lines) - 1)]
    texts["tests/test_contract.py"] = "\n".join(lines)
    if module == "numpy.testing":
        assert check(record, texts).passed
    else:
        with pytest.raises(ValueError, match="protected assertion reachable"):
            check(record, texts)


def test_inapplicable_case_requires_public_exclusion_and_still_occupies_its_tuple():
    record, texts = example()
    exclusion = "Both compiled wrappers with an interior name are outside this synthetic contract."
    texts["instruction.md"] += "\n" + exclusion
    case = record["condition_matrices"][0]["cases"][-1]
    case.update(
        result="inapplicable",
        fixture_input=None,
        expected_observation=None,
        test=None,
        fixture_evidence=[],
        expected_evidence=[],
    )
    with pytest.raises(ValidationError, match="cited public scope"):
        VerifierPreflightReviewV11.model_validate(record)
    case["inapplicable_evidence"] = [cite(exclusion)]
    assert check(record, texts).passed
    case["inapplicable_evidence"] = [cite("GOLD and mutations", "solution/solve.sh")]
    with pytest.raises(ValueError, match="public-contract citation"):
        check(record, texts)


@pytest.mark.parametrize(
    "change, message",
    [
        ("empty", "at least 1 item"),
        ("one_axis", "two to four distinct axes"),
        ("duplicate_axis", "two to four distinct axes"),
        ("duplicate_value", "distinct nonempty public categories"),
        ("unknown_axis", "exactly these axes"),
        ("unknown_value", "undeclared axis values"),
        ("orphan_cases", "must have no cases"),
        ("large_product", "exceeds 32 joint cases"),
        ("large_inventory", "exceeds 64 total cases"),
    ],
)
def test_inventory_is_bounded_and_unambiguous(change, message):
    record, _ = example()
    matrix = record["condition_matrices"][0]
    if change == "empty":
        record["condition_matrices"] = []
    elif change == "one_axis":
        matrix["axes"] = matrix["axes"][:1]
    elif change == "duplicate_axis":
        matrix["axes"][1]["name"] = "source"
    elif change == "duplicate_value":
        matrix["axes"][0]["values"] = ["plain", "plain"]
    elif change == "unknown_axis":
        matrix["cases"][0]["values"]["extra"] = "plain"
    elif change == "unknown_value":
        matrix["cases"][0]["values"]["source"] = "unknown"
    elif change == "orphan_cases":
        matrix["axes"] = []
    elif change == "large_product":
        for axis in matrix["axes"]:
            axis["values"] = ["a", "b", "c", "d"]
    elif change == "large_inventory":
        record["condition_matrices"] = [copy.deepcopy(matrix) for _ in range(9)]
    with pytest.raises(ValidationError, match=message):
        VerifierPreflightReviewV11.model_validate(record)


@pytest.mark.asyncio
async def test_current_finalization_and_cache_require_complete_joint_inventory(
    tmp_path, monkeypatch
):
    record, texts = example()
    task, root = tmp_path / "task", tmp_path / "candidate"
    for name, text in texts.items():
        path = task / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    budget = Budget(tmp_path / "budget.json", 10)

    async def judge(**kwargs):
        for name in texts:
            await kwargs["handlers"]["read_evidence"](name)
        malformed = copy.deepcopy(record)
        malformed["condition_matrices"][0]["cases"].pop()
        feedback = kwargs["validate_final"](json.dumps(malformed))
        assert "omits joint combinations" in feedback
        assert "joint-condition" in feedback
        assert kwargs["validate_final"](json.dumps(record)) is None
        assert kwargs["max_cost"] == 4 and kwargs["max_turns"] == 10
        return {
            "messages": [{"role": "assistant", "content": json.dumps(record)}],
            "turns": 3,
            "cost": 0,
        }

    agent = AsyncMock(side_effect=judge)
    monkeypatch.setattr(verifier, "run_agent", agent)
    for _ in range(2):
        assert (
            await verifier.review_verifier(
                task, root, model="anthropic/claude-opus-5", budget=budget
            )
        ).passed
    agent.assert_awaited_once()
    path = next((root / "verifier-reviews").glob("*/result.json"))
    cached = json.loads(path.read_text())
    assert cached["identity"]["policy_version"] == 11
    del cached["review"]["condition_matrices"]
    path.write_text(json.dumps(cached))
    with pytest.raises(verifier.VerifierReviewError, match="condition_matrices"):
        await verifier.review_verifier(task, root, model="anthropic/claude-opus-5", budget=budget)
    agent.assert_awaited_once()


def test_prompt_preserves_scope_and_distinguishes_linkage_from_semantic_proof():
    assert "Do not invent new axes" in verifier.SYSTEM
    assert "cited public exclusion or impossibility" in verifier.SYSTEM
    assert "both-wrapped plus interior-name case" in verifier.SYSTEM
    assert "worker self-assertions" in verifier.SYSTEM
    assert "not semantic truth or" in verifier.SYSTEM
    assert "never admission" in verifier.SYSTEM
    assert "condition_matrices" in verifier.SYSTEM
    assert "axes" in ConditionMatrix.model_json_schema()["required"]
