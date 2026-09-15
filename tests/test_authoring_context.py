import json

from repo2rlenv.quality.authoring_context import bounded_context


def test_large_parametrized_evidence_is_sampled_without_changing_verifier_contract():
    identities = [f"test_value[{index}-" + "x" * 1000 + "]" for index in range(3000)]
    contract = {"FAIL_TO_PASS": identities, "PASS_TO_PASS": identities.copy()}
    evidence = {"title": "Handle nested values", "contrast": contract, "log": "Failure\n" * 100000}
    text = bounded_context(evidence)
    assert len(text) <= 80000
    result = json.loads(text)
    assert result["contrast"]["FAIL_TO_PASS"]["total_items"] == 3000
    assert result["contrast"]["FAIL_TO_PASS"]["omitted_items"] == 2992
    assert len(contract["FAIL_TO_PASS"]) == len(contract["PASS_TO_PASS"]) == 3000


def test_small_evidence_is_preserved_exactly():
    evidence = {"source": "def value(x): return x", "tests": ["test_one", "test_two"]}
    assert json.loads(bounded_context(evidence)) == evidence
