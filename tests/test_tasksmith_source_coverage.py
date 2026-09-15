"""Actionable profile feedback preserves the complete PR source contract."""

import pytest

from repo2rlenv.spec.recipe_options import PythonRepositoryProfile
from repo2rlenv.tasksmith.models import Profile
from repo2rlenv.tasksmith.runner import _validate_profile_source_coverage


def test_coverage_feedback_lists_all_missing_roots_without_expanding_tests():
    profile = Profile(
        reasoning="Inspected repository dependency metadata and the complete PR diff.",
        resource="cpu",
        options=PythonRepositoryProfile(
            source_paths=["src"],
            test_paths=["tests"],
            test_selectors=["tests/test_core.py"],
        ),
        dependency_inputs=["pyproject.toml"],
        upstream_test_rationale="Offline regression coverage of the changed public behavior.",
    )
    changed = ["src/core.py", "example.py", "scripts/compare_test_coverage.py"]
    with pytest.raises(ValueError) as failure:
        _validate_profile_source_coverage(profile, changed)
    message = str(failure.value)
    assert (
        "options.source_paths omits PR source changes: example.py, scripts/compare_test_coverage.py"
        in message
    )
    assert "does not require selecting or executing them as tests" in message
    assert profile.options.source_paths == ["src"]
    profile.options.source_paths = ["src", "example.py", "scripts"]
    _validate_profile_source_coverage(profile, changed)
    assert profile.options.test_selectors == ["tests/test_core.py"]
    profile.options.public_exclude = ["example.py"]
    with pytest.raises(ValueError, match=r"Profile hides PR source change example\.py"):
        _validate_profile_source_coverage(profile, changed)
