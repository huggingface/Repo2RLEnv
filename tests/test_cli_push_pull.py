"""push/pull CLI commands + the multi-source URI parser.

Mock the Hub/Harbor/git layers; we just verify the CLI wires arguments
through correctly across all four backends (HF / Harbor / GitHub / errors).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest import mock

import pytest

from repo2rlenv.cli import _Backend, _parse_dataset_uri, cmd_pull, cmd_push
from repo2rlenv.hub import PullResult, PushResult

# ----------------------------------------------------------------------------
# _parse_dataset_uri — multi-backend URI classifier
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "uri,backend,repo_id,rev",
    [
        # HF — owner/name forms
        ("AdithyaSK/click-r2e", _Backend.HF, "AdithyaSK/click-r2e", None),
        ("hf://AdithyaSK/click-r2e", _Backend.HF, "AdithyaSK/click-r2e", None),
        ("  hf://owner/name  ", _Backend.HF, "owner/name", None),  # whitespace
        # HF — with @revision
        ("AdithyaSK/click-r2e@v1.0", _Backend.HF, "AdithyaSK/click-r2e", "v1.0"),
        ("hf://owner/name@main", _Backend.HF, "owner/name", "main"),
        ("hf://owner/name@a1b2c3d", _Backend.HF, "owner/name", "a1b2c3d"),
        # Harbor — bare and org/name forms (registry uses org/name in practice)
        ("harbor://swe-bench", _Backend.HARBOR, "swe-bench", None),
        ("harbor://swe-bench@lite", _Backend.HARBOR, "swe-bench", "lite"),
        ("harbor://my-dataset@verified", _Backend.HARBOR, "my-dataset", "verified"),
        ("harbor://cookbook/test", _Backend.HARBOR, "cookbook/test", None),
        ("harbor://scale-ai/swe-atlas-qna", _Backend.HARBOR, "scale-ai/swe-atlas-qna", None),
        ("harbor://cais/swebenchpro@v1", _Backend.HARBOR, "cais/swebenchpro", "v1"),
        # GitHub scheme + full URL
        ("gh://AdithyaSK/r2e-tasks", _Backend.GITHUB, "AdithyaSK/r2e-tasks", None),
        ("gh://AdithyaSK/r2e-tasks@main", _Backend.GITHUB, "AdithyaSK/r2e-tasks", "main"),
        ("gh://AdithyaSK/r2e-tasks@v0.1", _Backend.GITHUB, "AdithyaSK/r2e-tasks", "v0.1"),
        ("https://github.com/AdithyaSK/r2e-tasks", _Backend.GITHUB, "AdithyaSK/r2e-tasks", None),
        (
            "https://github.com/AdithyaSK/r2e-tasks.git",
            _Backend.GITHUB,
            "AdithyaSK/r2e-tasks",
            None,
        ),
    ],
)
def test_parse_dataset_uri_known_forms(uri, backend, repo_id, rev):
    b, rid, r = _parse_dataset_uri(uri, flag="<dataset>")
    assert b == backend
    assert rid == repo_id
    assert r == rev


def test_parse_dataset_uri_bare_name_resolves_via_whoami():
    """Bare `name` → HF (`<whoami>/name`). Mocks the whoami call."""
    with (
        mock.patch("repo2rlenv.cli.resolve_hf_token", create=True),
        mock.patch("huggingface_hub.whoami", return_value={"name": "AdithyaSK"}),
        mock.patch("repo2rlenv.auth.resolve_hf_token", return_value="tok_xxx"),
    ):
        b, rid, r = _parse_dataset_uri("click-r2e", flag="<dataset>")
    assert b == _Backend.HF
    assert rid == "AdithyaSK/click-r2e"
    assert r is None


def test_parse_dataset_uri_bare_name_with_rev_resolves_via_whoami():
    with (
        mock.patch("huggingface_hub.whoami", return_value={"name": "AdithyaSK"}),
        mock.patch("repo2rlenv.auth.resolve_hf_token", return_value="tok_xxx"),
    ):
        b, rid, r = _parse_dataset_uri("click-r2e@v1.0", flag="<dataset>")
    assert b == _Backend.HF
    assert rid == "AdithyaSK/click-r2e"
    assert r == "v1.0"


def test_parse_dataset_uri_bare_name_no_token_errors_clearly():
    with mock.patch("repo2rlenv.auth.resolve_hf_token", return_value=None):
        with pytest.raises(SystemExit, match="cannot auto-resolve owner"):
            _parse_dataset_uri("orphan-name", flag="<dataset>")


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "hf://",
        "owner/",
        "/name",
        "owner/name/extra",
        "gh://owner",  # missing repo
        "harbor://",
        "harbor://org/name/extra",  # harbor max depth is org/name
        "https://github.com/orphan",  # missing repo half
    ],
)
def test_parse_dataset_uri_rejects_malformed(uri):
    with pytest.raises(SystemExit):
        _parse_dataset_uri(uri, flag="<dataset>")


# ----------------------------------------------------------------------------
# cmd_push — HF only; other backends emit friendly errors
# ----------------------------------------------------------------------------


def _ns(**kwargs):
    return argparse.Namespace(**kwargs)


def _push_args(local_dir, dataset, private=False, message=None):
    return _ns(local_dir=str(local_dir), dataset=dataset, private=private, message=message)


def test_cmd_push_calls_push_to_hub_for_hf_uri(tmp_path: Path):
    dataset_dir = tmp_path / "ds"
    (dataset_dir / "task-1").mkdir(parents=True)
    (dataset_dir / "task-1" / "task.toml").write_text('[task]\nname="x"\n')

    fake = PushResult(
        repo_id="AdithyaSK/click-r2e",
        commit_sha="abc",
        registry_url="",
        task_count=1,
    )
    with mock.patch("repo2rlenv.hub.push_to_hub", return_value=fake) as m:
        rc = cmd_push(_push_args(dataset_dir, "hf://AdithyaSK/click-r2e"))
    assert rc == 0
    assert m.call_args.kwargs["repo_id"] == "AdithyaSK/click-r2e"


def test_cmd_push_rejects_harbor_uri(tmp_path: Path):
    dataset_dir = tmp_path / "ds"
    (dataset_dir / "task-1").mkdir(parents=True)
    (dataset_dir / "task-1" / "task.toml").write_text("[task]\n")
    rc = cmd_push(_push_args(dataset_dir, "harbor://my-dataset"))
    assert rc == 2  # friendly redirect, not a generic error


def test_cmd_push_rejects_github_uri(tmp_path: Path):
    dataset_dir = tmp_path / "ds"
    (dataset_dir / "task-1").mkdir(parents=True)
    (dataset_dir / "task-1" / "task.toml").write_text("[task]\n")
    rc = cmd_push(_push_args(dataset_dir, "gh://owner/repo"))
    assert rc == 2


def test_cmd_push_rejects_missing_local_dir(tmp_path: Path):
    rc = cmd_push(_push_args(tmp_path / "missing", "AdithyaSK/x"))
    assert rc == 2


def test_cmd_push_propagates_private_and_message(tmp_path: Path):
    dataset_dir = tmp_path / "ds"
    (dataset_dir / "task-1").mkdir(parents=True)
    (dataset_dir / "task-1" / "task.toml").write_text("[task]\n")

    fake = PushResult(repo_id="me/x", commit_sha="x", registry_url="", task_count=1)
    with mock.patch("repo2rlenv.hub.push_to_hub", return_value=fake) as m:
        cmd_push(_push_args(dataset_dir, "me/x", private=True, message="custom"))
    kwargs = m.call_args.kwargs
    assert kwargs["private"] is True
    assert kwargs["commit_message"] == "custom"


# ----------------------------------------------------------------------------
# cmd_pull — dispatcher routes to the right backend
# ----------------------------------------------------------------------------


def _pull_args(dataset, local_dir=None, task=None, force=False, registry_url=None):
    return _ns(
        dataset=dataset,
        local_dir=str(local_dir) if local_dir is not None else None,
        task=task,
        force=force,
        registry_url=registry_url,
    )


def test_cmd_pull_hf_route(tmp_path: Path):
    target = tmp_path / "out"
    fake = PullResult(
        repo_id="AdithyaSK/click-r2e",
        local_dir=target,
        task_count=4,
        snapshot_path=target,
    )
    with mock.patch("repo2rlenv.hub.pull_from_hub", return_value=fake) as m:
        rc = cmd_pull(_pull_args("hf://AdithyaSK/click-r2e", local_dir=target))
    assert rc == 0
    kw = m.call_args.kwargs
    assert kw["repo_id"] == "AdithyaSK/click-r2e"
    assert kw["revision"] is None


def test_cmd_pull_hf_route_with_revision(tmp_path: Path):
    target = tmp_path / "out"
    fake = PullResult(repo_id="o/n", local_dir=target, task_count=1, snapshot_path=target)
    with mock.patch("repo2rlenv.hub.pull_from_hub", return_value=fake) as m:
        cmd_pull(_pull_args("o/n@v2", local_dir=target))
    assert m.call_args.kwargs["revision"] == "v2"


def test_cmd_pull_harbor_route(tmp_path: Path):
    target = tmp_path / "out"
    fake = PullResult(
        repo_id="swe-bench@lite", local_dir=target, task_count=10, snapshot_path=target
    )
    with mock.patch("repo2rlenv.hub.pull_from_harbor", return_value=fake) as m:
        rc = cmd_pull(_pull_args("harbor://swe-bench@lite", local_dir=target))
    assert rc == 0
    kw = m.call_args.kwargs
    assert kw["name"] == "swe-bench"
    assert kw["tag"] == "lite"


def test_cmd_pull_harbor_route_with_custom_registry(tmp_path: Path):
    target = tmp_path / "out"
    fake = PullResult(repo_id="x", local_dir=target, task_count=1, snapshot_path=target)
    with mock.patch("repo2rlenv.hub.pull_from_harbor", return_value=fake) as m:
        cmd_pull(_pull_args("harbor://x", local_dir=target, registry_url="https://reg.example"))
    assert m.call_args.kwargs["registry_url"] == "https://reg.example"


def test_cmd_pull_github_route(tmp_path: Path):
    target = tmp_path / "out"
    fake = PullResult(
        repo_id="gh://AdithyaSK/r2e-tasks",
        local_dir=target,
        task_count=3,
        snapshot_path=target,
    )
    with mock.patch("repo2rlenv.hub.pull_from_github", return_value=fake) as m:
        rc = cmd_pull(_pull_args("gh://AdithyaSK/r2e-tasks", local_dir=target))
    assert rc == 0
    kw = m.call_args.kwargs
    assert kw["owner_repo"] == "AdithyaSK/r2e-tasks"
    assert kw["ref"] is None


def test_cmd_pull_github_route_with_ref(tmp_path: Path):
    target = tmp_path / "out"
    fake = PullResult(repo_id="x", local_dir=target, task_count=1, snapshot_path=target)
    with mock.patch("repo2rlenv.hub.pull_from_github", return_value=fake) as m:
        cmd_pull(_pull_args("gh://owner/repo@main", local_dir=target))
    assert m.call_args.kwargs["ref"] == "main"


def test_cmd_pull_github_full_url(tmp_path: Path):
    """`https://github.com/owner/repo` is also accepted and routes to GitHub backend."""
    target = tmp_path / "out"
    fake = PullResult(repo_id="x", local_dir=target, task_count=1, snapshot_path=target)
    with mock.patch("repo2rlenv.hub.pull_from_github", return_value=fake) as m:
        cmd_pull(_pull_args("https://github.com/owner/repo", local_dir=target))
    assert m.call_args.kwargs["owner_repo"] == "owner/repo"


def test_cmd_pull_returns_nonzero_on_backend_failure(tmp_path: Path):
    """HF backend raising should surface as exit code 1 with friendly error."""
    with mock.patch("repo2rlenv.hub.pull_from_hub", side_effect=RuntimeError("repo not found")):
        rc = cmd_pull(_pull_args("owner/missing", local_dir=tmp_path / "out"))
    assert rc == 1


def test_cmd_pull_default_target_dir_when_none(tmp_path: Path, monkeypatch):
    """When `local_dir` arg is None, default to ./datasets/<owner>__<name>."""
    fake = PullResult(
        repo_id="owner/ds",
        local_dir=Path("ignored"),
        task_count=1,
        snapshot_path=Path("ignored"),
    )
    monkeypatch.chdir(tmp_path)
    with mock.patch("repo2rlenv.hub.pull_from_hub", return_value=fake) as m:
        cmd_pull(_pull_args("owner/ds", local_dir=None))
    target = m.call_args.kwargs["local_dir"]
    assert "datasets" in str(target)
    assert "owner__ds" in str(target)
