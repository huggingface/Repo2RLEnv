"""Private verifier selection contracts; repository execution is mocked."""

import pytest
from pydantic import ValidationError

from repo2rlenv.quality.test_results import TestResults
from repo2rlenv.spec.recipe_options import PythonRepositoryProfile
from repo2rlenv.tasksmith import worker
from repo2rlenv.tasksmith.models import Design, Profile


def design(**overrides):
    return Design(
        **{
            "instruction": "Implement the requested public feature while preserving existing behavior. "
            * 2,
            "requirements": [
                {
                    "behavior": "The new public API computes the requested value.",
                    "verification": "Call the API inside a test and compare an independent expected value.",
                    "source_evidence": "The pinned PR introduces this public API.",
                }
            ],
            "verifier_rationale": "Private tests preserve the upstream assertions without collection-time imports.",
            "wrong_solution_ideas": ["Return the wrong value."],
            "valid_alternative_ideas": ["Compute the same value another way."],
            **overrides,
        }
    )


@pytest.mark.parametrize("text", ["", " \n\t"])
def test_replacement_cannot_remove_every_grading_test(text):
    with pytest.raises(ValidationError, match="requires private tests"):
        design(upstream_test_policy="replace", additional_tests=text)
    assert design().upstream_test_policy == "retain"


@pytest.mark.parametrize("policy", ["retain", "replace"])
def test_selection_preserves_readiness_and_routes_both_controls(monkeypatch, tmp_path, policy):
    base = tmp_path / "base"
    (base / "lib").mkdir(parents=True)
    (base / "tests").mkdir()
    (base / "lib/core.py").write_text("value = 1\n")
    original = ["checks/test_upstream.py::test_feature"]
    profile = Profile(
        reasoning="The actual upstream tests passed on the merged revision.",
        resource="cpu",
        options=PythonRepositoryProfile(
            source_paths=["lib"],
            test_paths=["checks"],
            test_selectors=original,
        ),
        dependency_inputs=["pyproject.toml"],
        upstream_test_rationale="The upstream cases establish offline merged-head readiness.",
    )
    before = profile.model_dump()
    expected = ([] if policy == "replace" else original) + ["tests/tasksmith_behavior.py"]
    observed = []

    def test_image(image, options, output, *, replacements):
        assert image == "the-verified-merged-image"
        assert options.test_selectors == expected
        assert options.test_paths == ["checks", "tests"]
        assert "tests/tasksmith_behavior.py" in replacements
        observed.append(output.name)
        broken = output.name == "defective"
        assert ("lib/core.py" in replacements) == broken
        return TestResults(
            {"feature": "failed" if broken else "passed", "adjacent": "passed"},
            int(broken),
        )

    exported = {}

    def export(**kwargs):
        exported.update(kwargs)
        return kwargs["destination"] / kwargs["name"]

    monkeypatch.setattr(worker, "run", lambda *args, **kwargs: "")
    monkeypatch.setattr(worker, "test_image", test_image)
    monkeypatch.setattr(worker, "export_repository_task", export)
    monkeypatch.setattr(worker, "test_excerpts", lambda *args, **kwargs: [])
    output = tmp_path / "construct"
    output.mkdir()
    source = {
        "source_files": ["lib/core.py"],
        "source_diff": "a pinned source patch",
        "id": "fixture",
        "url": "https://example.org/repo/pull/1",
        "head": "h",
        "base": "b",
        "workspace_strategy": "head_minus_source_patch",
    }
    text = "def test_feature():\n    from lib.core import value\n    assert value == 1\n"
    result = worker.construct(
        source,
        profile,
        design(upstream_test_policy=policy, additional_tests=text),
        {"base": str(base), "image": "the-verified-merged-image"},
        output,
    )
    assert observed == ["healthy", "defective"]
    assert profile.model_dump() == before
    assert result["options"]["test_selectors"] == expected
    assert exported["metadata"]["upstream_test_policy"] == policy
    assert exported["verifier_source"] == {"tests/tasksmith_behavior.py": text.encode()}
    assert exported["contrast"] == {"FAIL_TO_PASS": ["feature"], "PASS_TO_PASS": ["adjacent"]}
