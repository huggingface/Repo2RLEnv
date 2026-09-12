from __future__ import annotations

import json

import pytest

from repo2rlenv.pipelines.recipes.history.selection import check_entities, entities
from repo2rlenv.pipelines.recipes.history.source import merged_pulls
from repo2rlenv.spec.options import parse_options
from repo2rlenv.spec.recipe_options import R2EGymOptions, SWENextOptions


def options(cls):
    return cls(source_paths=["pkg"], test_paths=["tests"])


def test_distinct_historical_selection_policies():
    source = {"pkg/core.py": ("def square(x):\n return x\n", "def square(x):\n return x*x\n")}
    tests = {"tests/test_core.py": ("", "def test_square():\n assert square(3) == 9\n")}
    result = check_entities(source, tests, options(R2EGymOptions))
    assert result["edited"] == ["pkg/core.py:square"]
    assert result["test_match"]
    unrelated = {"tests/test_core.py": ("", "def test_behavior():\n assert api() == 9\n")}
    with pytest.raises(ValueError, match="does not name"):
        check_entities(source, unrelated, options(R2EGymOptions))
    assert not check_entities(source, unrelated, options(SWENextOptions))["test_match"]
    added = {"pkg/core.py": ("", "def square(x):\n return x*x\n")}
    with pytest.raises(ValueError, match="bug-edit"):
        check_entities(added, tests, options(R2EGymOptions))
    assert check_entities(added, tests, options(SWENextOptions))["added"]


def test_docstrings_and_comments_do_not_create_behavior_changes():
    before = 'class Number:\n "old"\n def value(self):\n  "old"\n  return 1\n'
    after = before.replace('"old"', '"new"') + "# a comment\n"
    assert entities(before) == entities(after)
    with pytest.raises(ValueError, match="No non-docstring"):
        check_entities({"pkg/x.py": (before, after)}, {}, options(SWENextOptions))


def test_pr_input_records_merge_revision_and_caches_api_result(tmp_path, monkeypatch):
    calls = []

    def gh(args):
        calls.append(args)
        return json.dumps(
            {
                "number": 17,
                "merge_commit_sha": "a" * 40,
                "head": {"sha": "b" * 40},
                "title": "Fix square",
                "body": "Expected nine.",
                "html_url": "https://github.com/a/b/pull/17",
                "merged_at": "2026-09-01",
            }
        )

    monkeypatch.setattr("repo2rlenv.pipelines.recipes.history.source._run_gh", gh)
    settings = options(SWENextOptions).model_copy(update={"pr_numbers": [17]})
    receipt = tmp_path / "pulls.json"
    rows = merged_pulls("https://github.com/a/b", settings, receipt)
    assert rows[0]["head"] == "a" * 40
    assert merged_pulls("https://github.com/a/b", settings, receipt) == rows
    assert len(calls) == 1


@pytest.mark.parametrize(
    "recipe,family,cls",
    [("swe_next", "pr_runtime", SWENextOptions), ("r2e_gym", "commit_runtime", R2EGymOptions)],
)
def test_owned_history_options_do_not_change_native_defaults(recipe, family, cls):
    assert isinstance(
        parse_options(family, {"source_paths": ["pkg"], "test_paths": ["tests"]}, recipe=recipe),
        cls,
    )
    assert not isinstance(parse_options(family, {}, recipe="native"), cls)
