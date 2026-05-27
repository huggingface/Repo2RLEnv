"""C1 fix: push_to_hub reads pipeline + repo_source from task.toml.

Previously, calling push_to_hub via the standalone `repo2rlenv push` CLI
left the dataset card with empty pipeline + repo_source fields because
the caller didn't pass them. Now these are auto-read from the first task's
`[metadata.repo2env]` subtable.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from repo2rlenv.hub import _read_task_metadata, push_to_hub
from repo2rlenv.spec.input import AuthSpec


def _make_task(dir_path: Path, *, pipeline: str, repo: str, name: str = "task-1") -> None:
    """Write a minimal valid task layout into dir_path/<name>/."""
    task_dir = dir_path / name
    task_dir.mkdir()
    (task_dir / "task.toml").write_text(
        f"""version = "1.0"

[task]
name = "{name}"
org = "test"
description = "test"

[metadata.repo2env]
spec_version = "0.1.0"
pipeline = "{pipeline}"
repo = "{repo}"
""",
        encoding="utf-8",
    )
    (task_dir / "instruction.md").write_text("test", encoding="utf-8")
    sol = task_dir / "solution"
    sol.mkdir()
    (sol / "patch.diff").write_text("", encoding="utf-8")


class TestReadTaskMetadata:
    def test_basic(self, tmp_path: Path) -> None:
        _make_task(tmp_path, pipeline="pr_runtime", repo="pallets/click")
        meta = _read_task_metadata(tmp_path / "task-1" / "task.toml")
        assert meta["pipeline"] == "pr_runtime"
        assert meta["repo"] == "pallets/click"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert _read_task_metadata(tmp_path / "nope.toml") == {}

    def test_malformed_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.toml"
        f.write_text("not toml at all =", encoding="utf-8")
        assert _read_task_metadata(f) == {}

    def test_missing_subtable_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "no_r2e.toml"
        f.write_text('version = "1.0"\n[task]\nname = "x"\n', encoding="utf-8")
        assert _read_task_metadata(f) == {}


class TestPushAutoReadsMetadata:
    """C1 fix verification — `cmd_push`'s no-kwargs invocation works."""

    def _mock_hf_api(self) -> mock.MagicMock:
        api = mock.MagicMock()
        api.create_repo.return_value = None
        upload_op = mock.MagicMock()
        upload_op.oid = "abcdef123"
        api.upload_folder.return_value = upload_op
        api.upload_file.return_value = None
        return api

    def test_pipeline_and_repo_read_from_task(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_task(tmp_path, pipeline="pr_runtime", repo="pallets/click")
        monkeypatch.setattr("repo2rlenv.hub.resolve_hf_token", lambda _auth: "fake-token")

        captured_card: dict[str, str] = {}

        def fake_build_card(**kwargs: str) -> str:
            captured_card.update(kwargs)
            return "fake-card"

        monkeypatch.setattr("repo2rlenv.hub._build_dataset_card", fake_build_card)
        api = self._mock_hf_api()
        monkeypatch.setattr("huggingface_hub.HfApi", lambda token=None: api)

        # Note: NO pipeline / repo_source kwargs — mimics standalone cmd_push
        result = push_to_hub(tmp_path, "owner/click-r2e", AuthSpec())

        assert result.task_count == 1
        assert captured_card["pipeline"] == "pr_runtime"
        assert captured_card["repo_source"] == "pallets/click"

    def test_caller_override_wins(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Legacy callers passing pipeline explicitly should still control the card."""
        _make_task(tmp_path, pipeline="pr_runtime", repo="pallets/click")
        monkeypatch.setattr("repo2rlenv.hub.resolve_hf_token", lambda _auth: "fake-token")

        captured_card: dict[str, str] = {}

        def fake_build_card(**kwargs: str) -> str:
            captured_card.update(kwargs)
            return "fake-card"

        monkeypatch.setattr("repo2rlenv.hub._build_dataset_card", fake_build_card)
        api = self._mock_hf_api()
        monkeypatch.setattr("huggingface_hub.HfApi", lambda token=None: api)

        push_to_hub(
            tmp_path,
            "owner/click-r2e",
            AuthSpec(),
            pipeline="custom_pipeline",
            repo_source="custom/source",
        )
        assert captured_card["pipeline"] == "custom_pipeline"
        assert captured_card["repo_source"] == "custom/source"

    def test_falls_back_to_default_for_missing_metadata(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pre-v0.8.2.post3 dataset without [metadata.repo2env] still pushes."""
        # Hand-roll a task without the subtable
        (tmp_path / "task-1").mkdir()
        (tmp_path / "task-1" / "task.toml").write_text(
            'version = "1.0"\n[task]\nname = "task-1"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr("repo2rlenv.hub.resolve_hf_token", lambda _auth: "fake-token")
        captured: dict[str, str] = {}
        monkeypatch.setattr(
            "repo2rlenv.hub._build_dataset_card",
            lambda **kw: captured.update(kw) or "x",
        )
        api = self._mock_hf_api()
        monkeypatch.setattr("huggingface_hub.HfApi", lambda token=None: api)
        push_to_hub(tmp_path, "owner/legacy", AuthSpec())
        # Falls back to "pr_diff" default and empty repo_source
        assert captured["pipeline"] == "pr_diff"
        assert captured["repo_source"] == ""


def test_reward_doc_is_pipeline_specific():
    """pr_runtime card must document F2P/P2P, not pr_diff's LLM-judge."""
    from repo2rlenv.hub import _reward_doc_for

    rt = _reward_doc_for("pr_runtime")
    assert "f2p_rate" in rt and "resolved" in rt
    assert "LLM-judge" not in rt and "5-component" not in rt
    pd = _reward_doc_for("pr_diff")
    assert "6-component" in pd or "LLM-judge" in pd


def test_build_manifest_from_task_tomls(tmp_path: Path):
    """manifest.json is built from staged task.toml metadata, one row per task."""
    import json

    from repo2rlenv.hub import _build_manifest

    tasks = tmp_path / "tasks"
    for i in (1, 2):
        d = tasks / f"o__r-{i}"
        d.mkdir(parents=True)
        (d / "task.toml").write_text(
            "[metadata.repo2env]\n"
            'pipeline="pr_runtime"\nrepo="o/r"\nref="sha"\n'
            'reward_kinds=["test_execution"]\n'
            "[metadata.repo2env.pr_runtime]\n"
            f'pr_url="https://github.com/o/r/pull/{i}"\n'
            "[metadata.repo2env.reward_calibration]\n"
            f'f2p_count={i}\np2p_count=5\ndifficulty="small"\n'
        )
    m = json.loads(_build_manifest(tasks, repo_id="x/y", pipeline="pr_runtime"))
    assert m["task_count"] == 2
    assert {r["task_id"] for r in m["tasks"]} == {"o__r-1", "o__r-2"}
    assert all(r["repo"] == "o/r" and r["difficulty"] == "small" for r in m["tasks"])


def test_registry_and_manifest_cover_same_tasks(tmp_path: Path):
    """Schema/consistency guard: registry.json, manifest.json, and the task
    dirs must all reference the exact same task set."""
    import json

    from repo2rlenv.hub import _build_manifest, _build_registry_json

    tasks = tmp_path / "tasks"
    names = [f"o__r-{i}" for i in (1, 2, 3)]
    for n in names:
        d = tasks / n
        d.mkdir(parents=True)
        (d / "task.toml").write_text(
            f'[task]\nname="o/r-{n}"\n[metadata.repo2env]\npipeline="pr_runtime"\nrepo="o/r"\nref="s"\n'
        )
    man = {
        r["task_id"]
        for r in json.loads(_build_manifest(tasks, repo_id="x/y", pipeline="pr_runtime"))["tasks"]
    }
    reg = _build_registry_json(
        repo_id="x/y", commit_sha="abc", dataset_name="y", description="d", task_dirs=names
    )
    reg_names = {t["name"] for spec in reg for t in spec["tasks"]}
    assert man == set(names) == reg_names


def test_composition_block_renders_validation_and_skew():
    """An enriched manifest summary renders tracked/command resolution + skew."""
    from repo2rlenv.hub import _composition_block

    assert _composition_block(None) == ""  # plain push -> no section
    summary = {
        "task_count": 100,
        "validation": {
            "harbor_version": "0.6.6",
            "tracked_resolved": 100,
            "command_resolved": 88,
            "eval_grade": 87,
        },
        "repo_distribution": {"pallets/click": 28, "urfave/cli": 25},
    }
    out = _composition_block(summary)
    assert "Validation & composition" in out
    assert "`command_resolved`" in out and "88/100" in out
    assert "100/100" in out  # tracked
    assert "`eval_grade`" in out and "87/100" in out
    assert "Repo distribution" in out
    assert "[`pallets/click`](https://github.com/pallets/click) | 28" in out
    assert "eval_grade == true" in out  # strict-eval guidance


def test_push_preserves_enriched_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A source manifest.json with a `validation` block is preserved verbatim,
    not clobbered by push's auto-generated minimal manifest. Audit P1."""
    import json

    _make_task(tmp_path, pipeline="pr_runtime", repo="o/r")
    enriched = {
        "task_count": 1,
        "pipeline": "pr_runtime",
        "validation": {"tracked_resolved": 1, "command_resolved": 1, "harbor_version": "0.6.6"},
        "repo_distribution": {"o/r": 1},
        "tasks": [{"task_id": "task-1", "validation": {"resolved": True}}],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(enriched), encoding="utf-8")

    monkeypatch.setattr("repo2rlenv.hub.resolve_hf_token", lambda _auth: "fake-token")
    uploaded: dict[str, str] = {}

    class _Api:
        def create_repo(self, *a, **k): ...
        def upload_folder(self, *, folder_path, **k):
            staging = Path(folder_path)
            uploaded["manifest"] = (staging / "manifest.json").read_text()
            uploaded["readme"] = (staging / "README.md").read_text()

            class _Op:
                oid = "deadbeef"

            return _Op()

        def upload_file(self, *a, **k): ...

    monkeypatch.setattr("huggingface_hub.HfApi", lambda token=None: _Api())
    push_to_hub(tmp_path, "owner/ds", AuthSpec())

    m = json.loads(uploaded["manifest"])
    assert "validation" in m and m["validation"]["command_resolved"] == 1  # preserved
    assert "Validation & composition" in uploaded["readme"]  # card reflects it
