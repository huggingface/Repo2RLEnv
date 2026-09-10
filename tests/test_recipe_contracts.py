from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from repo2rlenv.cli import main
from repo2rlenv.spec.input import GenerationInput, RepositorySource


def config(**kwargs):
    return {
        "pipeline": {"name": "repo_mutate", "recipe": "swe_smith"},
        "output": {"destination": "workspace/tasks", "org": "tests", "dataset_name": "fixture"},
        **kwargs,
    }


def test_legacy_and_typed_repository_agree():
    old = GenerationInput.model_validate(config(repo={"url": "example/library"}))
    new = GenerationInput.model_validate(
        config(source={"kind": "repository", "repo": {"url": "example/library"}})
    )
    assert isinstance(old.source, RepositorySource)
    assert old == new
    assert GenerationInput.model_validate(old.model_dump(mode="json")) == old


def test_non_repository_inputs_do_not_invent_a_repository():
    value = config(source={"kind": "seeds", "path": "seeds.jsonl"})
    value["pipeline"] = {"name": "terminal_synth", "recipe": "tmax"}
    parsed = GenerationInput.model_validate(value)
    assert parsed.repo is None
    assert parsed.source_label == "seeds.jsonl"
    value["pipeline"]["recipe"] = "native"
    with pytest.raises(ValidationError, match="Native pipelines require a repository"):
        GenerationInput.model_validate(value)


def test_conflicting_sources_and_unknown_fields_rejected():
    with pytest.raises(ValidationError, match="different inputs"):
        GenerationInput.model_validate(
            config(repo={"url": "x/one"}, source={"kind": "repository", "repo": {"url": "x/two"}})
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        GenerationInput.model_validate(
            config(source={"kind": "seeds", "path": "data", "typo": True})
        )


def test_discovery_json_is_parseable_without_credentials(monkeypatch, capsys):
    monkeypatch.setattr("repo2rlenv.cli._load_dotenv_if_present", lambda: None)
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DAYTONA_API_KEY", "MODAL_TOKEN_ID"):
        monkeypatch.delenv(key, raising=False)
    assert main(["pipelines", "list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["recipes"]) == 15
    assert {item["recipe"] for item in data["native"]} == {"native"}


def test_recipe_family_mismatch_is_actionable(capsys):
    assert main(["pipelines", "describe", "terminal_synth", "--recipe", "swe_smith", "--json"]) == 2
    assert "repo_mutate" in json.loads(capsys.readouterr().out)["error"]
