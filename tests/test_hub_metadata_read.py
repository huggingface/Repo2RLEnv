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
