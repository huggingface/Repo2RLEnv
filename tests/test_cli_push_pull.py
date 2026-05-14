"""push/pull CLI commands + the `hf://` URI parser.

Mock the Hub layer; we just verify the CLI wires arguments through correctly
and that the URI-parse helper handles all the variants we accept.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from repo2rlenv.cli import _parse_hf_uri, cmd_pull, cmd_push
from repo2rlenv.hub import PullResult, PushResult

# ----------------------------------------------------------------------------
# _parse_hf_uri — URI accepted in two forms
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "uri,expected",
    [
        ("hf://AdithyaSK/click-r2e", "AdithyaSK/click-r2e"),
        ("AdithyaSK/click-r2e", "AdithyaSK/click-r2e"),
        ("hf://huggingface/datasets-launch", "huggingface/datasets-launch"),
        ("  hf://owner/name  ", "owner/name"),  # tolerates whitespace
    ],
)
def test_parse_hf_uri_accepts_valid(uri, expected):
    assert _parse_hf_uri(uri, flag="<dataset>") == expected


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "no-slash",
        "hf://",
        "hf://only-one",
        "owner/",
        "/name",
        "hf://owner/name/extra",
    ],
)
def test_parse_hf_uri_rejects_malformed(uri):
    with pytest.raises(SystemExit, match="<dataset>"):
        _parse_hf_uri(uri, flag="<dataset>")


# ----------------------------------------------------------------------------
# cmd_push — wires arguments through to hub.push_to_hub
# ----------------------------------------------------------------------------


def _ns(**kwargs):
    """Build an argparse.Namespace-ish for cmd_* under test."""
    import argparse

    return argparse.Namespace(**kwargs)


def test_cmd_push_calls_push_to_hub(tmp_path: Path):
    dataset_dir = tmp_path / "click-e2e"
    (dataset_dir / "task-1").mkdir(parents=True)
    (dataset_dir / "task-1" / "task.toml").write_text('[task]\nname="x"\n')

    fake_result = PushResult(
        repo_id="AdithyaSK/click-r2e",
        commit_sha="abc123",
        registry_url="https://huggingface.co/datasets/AdithyaSK/click-r2e/resolve/main/registry.json",
        task_count=1,
    )

    with mock.patch("repo2rlenv.hub.push_to_hub", return_value=fake_result) as m:
        rc = cmd_push(
            _ns(
                local_dir=str(dataset_dir),
                dataset="hf://AdithyaSK/click-r2e",
                private=False,
                message=None,
            )
        )
    assert rc == 0
    m.assert_called_once()
    kwargs = m.call_args.kwargs
    assert kwargs["repo_id"] == "AdithyaSK/click-r2e"
    assert kwargs["private"] is False


def test_cmd_push_rejects_missing_local_dir(tmp_path: Path):
    rc = cmd_push(
        _ns(
            local_dir=str(tmp_path / "does-not-exist"),
            dataset="hf://owner/name",
            private=False,
            message=None,
        )
    )
    assert rc == 2


def test_cmd_push_propagates_private_flag(tmp_path: Path):
    dataset_dir = tmp_path / "ds"
    (dataset_dir / "task-1").mkdir(parents=True)
    (dataset_dir / "task-1" / "task.toml").write_text("[task]\n")

    fake_result = PushResult(repo_id="me/private-ds", commit_sha="x", registry_url="", task_count=1)
    with mock.patch("repo2rlenv.hub.push_to_hub", return_value=fake_result) as m:
        cmd_push(
            _ns(
                local_dir=str(dataset_dir),
                dataset="me/private-ds",
                private=True,
                message="custom",
            )
        )
    kwargs = m.call_args.kwargs
    assert kwargs["private"] is True
    assert kwargs["commit_message"] == "custom"


def test_cmd_push_returns_nonzero_on_hub_failure(tmp_path: Path):
    dataset_dir = tmp_path / "ds"
    (dataset_dir / "task-1").mkdir(parents=True)
    (dataset_dir / "task-1" / "task.toml").write_text("[task]\n")

    with mock.patch("repo2rlenv.hub.push_to_hub", side_effect=RuntimeError("no token")):
        rc = cmd_push(
            _ns(
                local_dir=str(dataset_dir),
                dataset="me/x",
                private=False,
                message=None,
            )
        )
    assert rc == 1


# ----------------------------------------------------------------------------
# cmd_pull — wires arguments through to hub.pull_from_hub
# ----------------------------------------------------------------------------


def test_cmd_pull_calls_pull_from_hub(tmp_path: Path):
    target = tmp_path / "out"
    fake_result = PullResult(
        repo_id="AdithyaSK/click-r2e",
        local_dir=target,
        task_count=4,
        snapshot_path=target,
    )
    with mock.patch("repo2rlenv.hub.pull_from_hub", return_value=fake_result) as m:
        rc = cmd_pull(
            _ns(
                dataset="hf://AdithyaSK/click-r2e",
                local_dir=str(target),
                task=None,
                force=False,
            )
        )
    assert rc == 0
    kwargs = m.call_args.kwargs
    assert kwargs["repo_id"] == "AdithyaSK/click-r2e"
    assert Path(kwargs["local_dir"]).resolve() == target.resolve()
    assert kwargs["task"] is None
    assert kwargs["force"] is False


def test_cmd_pull_default_target_when_local_dir_omitted():
    """When `local_dir` arg is None, default to ./datasets/<owner>__<dataset>."""
    fake_result = PullResult(
        repo_id="owner/ds",
        local_dir=Path("ignored"),
        task_count=1,
        snapshot_path=Path("ignored"),
    )
    with mock.patch("repo2rlenv.hub.pull_from_hub", return_value=fake_result) as m:
        cmd_pull(_ns(dataset="hf://owner/ds", local_dir=None, task=None, force=False))
    kwargs = m.call_args.kwargs
    # Path ends with the expected default naming pattern
    assert str(kwargs["local_dir"]).endswith("datasets/owner__ds")


def test_cmd_pull_forwards_task_filter_and_force():
    fake_result = PullResult(
        repo_id="owner/ds",
        local_dir=Path("/tmp/x"),
        task_count=1,
        snapshot_path=Path("/tmp/x"),
    )
    with mock.patch("repo2rlenv.hub.pull_from_hub", return_value=fake_result) as m:
        cmd_pull(
            _ns(
                dataset="hf://owner/ds",
                local_dir="/tmp/x",
                task="pallets__click-3373",
                force=True,
            )
        )
    kwargs = m.call_args.kwargs
    assert kwargs["task"] == "pallets__click-3373"
    assert kwargs["force"] is True


def test_cmd_pull_returns_nonzero_on_hub_failure():
    with mock.patch("repo2rlenv.hub.pull_from_hub", side_effect=RuntimeError("repo not found")):
        rc = cmd_pull(_ns(dataset="hf://owner/missing", local_dir=None, task=None, force=False))
    assert rc == 1
