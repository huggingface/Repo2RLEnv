from __future__ import annotations

import json

import pytest

from repo2rlenv.pipelines.recipes.swe_gen.source import fetch_source
from repo2rlenv.pipelines.recipes.swe_smith.export import export_repository_task
from repo2rlenv.spec.recipe_options import PRRecipeOptions


@pytest.fixture
def profile():
    return PRRecipeOptions(source_paths=["lib"], test_paths=["tests"])


def test_source_keeps_tests_separate_and_pins_head(monkeypatch, profile):
    source_diff = "diff --git a/lib/one.py b/lib/one.py\n--- a/lib/one.py\n+++ b/lib/one.py\n@@ -1 +1 @@\n-return 0\n+return 1\n"
    test_diff = "diff --git a/tests/test_one.py b/tests/test_one.py\n--- a/tests/test_one.py\n+++ b/tests/test_one.py\n@@ -1 +1 @@\n-assert one() == 0\n+assert one() == 1\n"

    def api(args):
        if "/files?" in args[1]:
            return json.dumps(
                [
                    [
                        {"filename": "lib/one.py", "status": "modified"},
                        {"filename": "tests/test_one.py", "status": "modified"},
                    ]
                ]
            )
        return json.dumps(
            {
                "merged_at": "2026-01-01",
                "base": {"sha": "a" * 40, "repo": {"private": False}},
                "head": {"sha": "b" * 40},
                "title": "Correct one",
                "body": "",
            }
        )

    monkeypatch.setattr("repo2rlenv.pipelines.recipes.swe_gen.source._run_gh", api)
    monkeypatch.setattr(
        "repo2rlenv.pipelines.recipes.swe_gen.source.fetch_pr_diff",
        lambda *args: source_diff + test_diff,
    )
    result = fetch_source("https://github.com/example/library/pull/1", profile)
    assert result["source_diff"] == source_diff
    assert result["test_files"] == ["tests/test_one.py"]
    assert result["head"] == "b" * 40


def test_multiple_references_are_private_and_restore_each_file(tmp_path, profile):
    base = tmp_path / "base"
    (base / "lib").mkdir(parents=True)
    (base / "tests").mkdir()
    reference = {"lib/one.py": b"answer = 1111\n", "lib/two.py": b"answer = 2222\n"}
    for relative, content in reference.items():
        (base / relative).write_bytes(content)
    (base / "tests/test_one.py").write_text("def test_one(): pass\n")
    task = export_repository_task(
        base=base,
        defective={key: b"answer = 0\n" for key in reference},
        reference=reference,
        options=profile,
        instruction="Restore the public behavior.",
        destination=tmp_path / "tasks",
        name="swe-gen-fixture",
        org="test",
        contrast={"FAIL_TO_PASS": ["tests.test_one::test_one"], "PASS_TO_PASS": []},
        metadata={"recipe": "swe_gen", "recipe_version": "1"},
    )
    assert not (task / "environment/source/tests").exists()
    for relative, content in reference.items():
        assert (task / "solution/reference" / relative).read_bytes() == content
        assert (task / "environment/source" / relative).read_bytes() == b"answer = 0\n"
        assert (task / "tests/source" / relative).read_bytes() == b"answer = 0\n"
        assert f"/workspace/{relative}" in (task / "solution/solve.sh").read_text()
    harbor = pytest.importorskip("harbor.models.task.task")
    assert harbor.Task(task).config.verifier.environment_mode.value == "separate"
