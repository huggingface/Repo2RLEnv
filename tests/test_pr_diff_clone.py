"""The emitted clone must use the mined repository's host and build-time token."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from repo2rlenv.emitter.harbor import write_harbor_task
from repo2rlenv.github import PullRequestSummary
from repo2rlenv.pipelines.pr_diff import PRDiffPipeline, build_pr_diff_environment_dockerfile
from repo2rlenv.spec.input import GenerationInput, OutputSpec, PipelineSpec, RepoSpec
from repo2rlenv.spec.options import PRDiffOptions


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("example/repo", "https://github.com/example/repo.git"),
        ("https://github.com/example/repo.git", "https://github.com/example/repo.git"),
        ("git@github.com:example/repo.git", "https://github.com/example/repo.git"),
        (
            "https://gitlab.com/python-devs/importlib_resources",
            "https://gitlab.com/python-devs/importlib_resources.git",
        ),
        ("https://gitlab.com/group/repo.git", "https://gitlab.com/group/repo.git"),
        ("git@gitlab.com:group/repo.git", "https://gitlab.com/group/repo.git"),
        ("https://user:embedded-secret@gitlab.com/group/repo", "https://gitlab.com/group/repo.git"),
        ("http://gitlab.com/group/repo", "https://gitlab.com/group/repo.git"),
        (
            "https://gitlab.com/group/subgroup/repo/",
            "https://gitlab.com/group/subgroup/repo.git",
        ),
    ],
)
def test_emitted_task_preserves_clone_host_and_path(tmp_path: Path, source, expected):
    config = GenerationInput(
        repo=RepoSpec(url=source, access="public"),
        pipeline=PipelineSpec(name="pr_diff"),
        output=OutputSpec(destination=str(tmp_path), org="review", dataset_name="test"),
    )
    pr = PullRequestSummary(
        number=1,
        title="Fix the result",
        body="Return the correct result.",
        state="closed",
        merged_at="2026-01-01T00:00:00Z",
        base_ref="main",
        base_sha="0" * 40,
        head_sha="1" * 40,
        is_draft=False,
        url="https://example.com/change/1",
        changed_files=["calc.py"],
    )
    diff = "diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    task = PRDiffPipeline(config, PRDiffOptions())._build_task(pr, diff)
    task_dir = write_harbor_task(task, tmp_path)
    dockerfile = (task_dir / "environment/Dockerfile").read_text()
    assert f"git clone --filter=blob:none {expected} /workspace" in dockerfile
    assert f"remote set-url origin {expected}" in dockerfile
    assert f"git reset --hard {pr.base_sha}" in dockerfile
    assert "embedded-secret" not in dockerfile
    if expected.startswith("https://gitlab.com/"):
        assert "ARG GITLAB_TOKEN=" in dockerfile
        assert "GITHUB_TOKEN" not in dockerfile
        assert "@github.com/" not in dockerfile


@pytest.mark.parametrize(
    ("host", "token_arg", "username", "other_arg"),
    [
        ("github.com", "GITHUB_TOKEN", "x-access-token", "GITLAB_TOKEN"),
        ("gitlab.com", "GITLAB_TOKEN", "oauth2", "GITHUB_TOKEN"),
    ],
)
@pytest.mark.parametrize("token", ["", "dummy-token", "dummy token * $HOME $(false)"])
def test_clone_shell_uses_correct_token_and_scrubs_origin(
    tmp_path: Path, monkeypatch, host, token_arg, username, other_arg, token
):
    # Ambient credentials must not be embedded in the generated Dockerfile.
    monkeypatch.setenv(token_arg, "generation-token-must-not-be-emitted")
    repo_url = f"https://{host}/group/repo.git"
    dockerfile = build_pr_diff_environment_dockerfile(
        repo_url=repo_url,
        base_commit="0" * 40,
        oracle_diff="",
        instruction="Fix the result",
    )
    assert f"ARG {token_arg}=\n" in dockerfile
    assert other_arg not in dockerfile
    assert "generation-token-must-not-be-emitted" not in dockerfile

    # Execute the actual RUN body using a fake git binary: no network or Docker.
    clone_script = dockerfile.split("RUN if ", 1)[1].split("\nWORKDIR", 1)[0]
    clone_script = "if " + clone_script
    calls = tmp_path / "git-calls.jsonl"
    recorder = tmp_path / "record.py"
    recorder.write_text(
        "import json, os, sys\n"
        "with open(os.environ['CLONE_TEST_CALLS'], 'a') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    fake_git = tmp_path / "git"
    fake_git.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(recorder))} "$@"\n'
    )
    fake_git.chmod(0o755)
    result = subprocess.run(
        ["/bin/sh", "-c", clone_script],
        env=dict(
            os.environ,
            PATH=f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
            CLONE_TEST_CALLS=str(calls),
            **{token_arg: token, other_arg: "wrong-provider-token"},
        ),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    clone_url = f"https://{username}:{token}@{host}/group/repo.git" if token else repo_url
    assert [json.loads(line) for line in calls.read_text().splitlines()] == [
        ["clone", "--filter=blob:none", clone_url, "/workspace"],
        ["-C", "/workspace", "remote", "set-url", "origin", repo_url],
    ]
