"""Anti-contamination env-guard helpers (egress denylist + git-history scrub)."""

from __future__ import annotations

import ast
import importlib
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from repo2rlenv.pipelines._env_guard import (
    FIX_SOURCE_HOSTS,
    GITLAB_FIX_SOURCE_HOSTS,
    egress_guard_compose,
    fix_source_hosts,
    git_history_scrub,
)
from repo2rlenv.sources import SourceKind


def test_egress_guard_blackholes_fix_source_hosts():
    compose = egress_guard_compose()
    # PyPI + GitHub + their CDNs must be mapped to 0.0.0.0 on the agent service.
    for host in ("pypi.org", "files.pythonhosted.org", "github.com", "raw.githubusercontent.com"):
        assert f'"{host}:0.0.0.0"' in compose, f"{host} not blackholed"
    assert "services:" in compose and "main:" in compose and "extra_hosts:" in compose


def test_egress_guard_is_valid_yaml_with_extra_hosts():
    import yaml  # pyyaml is a transitive dep; skip cleanly if absent

    data = yaml.safe_load(egress_guard_compose())
    eh = data["services"]["main"]["extra_hosts"]
    assert len(eh) == len(FIX_SOURCE_HOSTS)
    assert all(entry.endswith(":0.0.0.0") for entry in eh)


def test_egress_guard_keeps_model_api_reachable():
    # The guard must NOT blackhole the LLM API or the agent's installer hosts,
    # or a hosted agent (claude-code) could not run at all.
    compose = egress_guard_compose()
    for allowed in ("api.anthropic.com", "claude.ai", "registry.npmjs.org", "deb.debian.org"):
        assert allowed not in compose


def test_git_history_scrub_removes_remote_and_prunes():
    lines = git_history_scrub("deadbeefcafe")
    assert "git remote remove origin" in lines
    assert "git reflog expire --expire=now --all" in lines
    assert "git gc --prune=now" in lines
    # base_commit must stay reachable (verifier resets test files against it)
    assert "git checkout -q -B base deadbeefcafe" in lines


def test_build_environment_dockerfile_includes_scrub_and_keeps_base():
    from repo2rlenv.pipelines.pr_runtime import build_environment_dockerfile

    df = build_environment_dockerfile("local/r2e-bootstrap/x:abc", "9d2747057c4a")
    assert "git reset --hard 9d2747057c4a" in df  # working tree at base
    assert "git remote remove origin" in df  # future pruned
    assert "git gc --prune=now" in df


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


@pytest.mark.skipif(shutil.which("git") is None or os.name == "nt", reason="needs git and sh")
@pytest.mark.parametrize("clone_args", [["--filter=blob:none"], []], ids=["partial", "full"])
def test_git_history_scrub_leaves_no_later_objects(tmp_path, monkeypatch, clone_args):
    """Run the scrub's RUN lines on a clone of base -> fix; only base may survive."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "t")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "t@example.com")
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "uploadpack.allowFilter", "true")
    (upstream / "lib.py").write_text("def f():\n    return 1\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-qm", "base")
    base = _git(upstream, "rev-parse", "HEAD").stdout.strip()
    (upstream / "lib.py").write_text("def f():\n    return 2  # the fix\n")
    _git(upstream, "commit", "-qam", "Fix f")
    fix = _git(upstream, "rev-parse", "HEAD").stdout.strip()

    workspace = tmp_path / "workspace"
    cloned = _git(tmp_path, "clone", "-q", *clone_args, f"file://{upstream}", str(workspace))
    assert cloned.returncode == 0, cloned.stderr
    assert _git(workspace, "reset", "-q", "--hard", base).returncode == 0
    for run in re.split(r"^RUN ", git_history_scrub(base), flags=re.MULTILINE):
        step = run.replace("\\\n", " ").strip()
        if step:
            done = subprocess.run(["sh", "-c", step], cwd=workspace, capture_output=True, text=True)
            assert done.returncode == 0, (step, done.stderr)

    assert _git(workspace, "cat-file", "-e", fix).returncode != 0
    objects = subprocess.run(
        ["git", "cat-file", "--batch-all-objects", "--batch"], cwd=workspace, capture_output=True
    )
    assert b"the fix" not in objects.stdout
    commits = _git(workspace, "cat-file", "--batch-all-objects", "--batch-check")
    assert [line.split()[0] for line in commits.stdout.splitlines() if " commit " in line] == [base]
    # The verifier still resets files against base and diffs against it.
    assert _git(workspace, "checkout", base, "--", "lib.py").returncode == 0
    assert _git(workspace, "diff", "--quiet", base).returncode == 0
    assert _git(workspace, "fsck", "--no-progress").returncode == 0


@pytest.mark.parametrize(
    ("path", "kept"),
    [
        # Written by the bootstrap's test run at the tip, where the fix and its
        # hidden test already exist: compiled source, compiled tests, node IDs.
        ("pkg/__pycache__/mod.cpython-312.pyc", False),
        ("tests/__pycache__/test_new.cpython-312-pytest-9.1.1.pyc", False),
        (".pytest_cache/v/cache/nodeids", False),
        # Installed dependencies stay so the suite still runs at base_commit.
        (".venv/lib/site.py", True),
        ("node_modules/dep/index.js", True),
    ],
)
def test_build_environment_dockerfile_clean_drops_bootstrap_caches(tmp_path, path, kept):
    from repo2rlenv.pipelines.pr_runtime import build_environment_dockerfile

    df = build_environment_dockerfile("local/r2e-bootstrap/x:abc", "9d2747057c4a")
    clean = next(line for line in df.splitlines() if "git clean" in line)
    clean = clean.split("&& ", 1)[1]

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    leftover = tmp_path / path
    leftover.parent.mkdir(parents=True)
    leftover.write_text("from the bootstrap-time tip")
    subprocess.run(shlex.split(clean), cwd=tmp_path, check=True, capture_output=True)

    assert leftover.exists() is kept


# --- source-aware host list ---------------------------------------------------


def test_gitlab_sources_blackhole_gitlab_as_well():
    hosts = fix_source_hosts(SourceKind.GITLAB)
    assert "gitlab.com" in hosts
    # The base list still applies: a GitLab repo can publish to PyPI too.
    assert set(FIX_SOURCE_HOSTS).issubset(hosts)
    compose = egress_guard_compose(hosts)
    for host in GITLAB_FIX_SOURCE_HOSTS:
        assert f'"{host}:0.0.0.0"' in compose


@pytest.mark.parametrize("kind", [SourceKind.GITHUB, SourceKind.LOCAL, None])
def test_other_sources_keep_the_base_list(kind):
    assert fix_source_hosts(kind) == FIX_SOURCE_HOSTS


def test_gitlab_compose_is_valid_yaml():
    import yaml  # pyyaml is a transitive dep; skip cleanly if absent

    hosts = fix_source_hosts(SourceKind.GITLAB)
    eh = yaml.safe_load(egress_guard_compose(hosts))["services"]["main"]["extra_hosts"]
    assert len(eh) == len(hosts)
    assert "gitlab.com:0.0.0.0" in eh


@pytest.mark.parametrize("module", ["pr_runtime", "commit_runtime", "cve_patches"])
def test_pipelines_pass_the_source_aware_host_list(module):
    """Every emitted task must blackhole its own forge, not just github.com."""
    source = Path(importlib.import_module(f"repo2rlenv.pipelines.{module}").__file__)
    tree = ast.parse(source.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "egress_guard_compose"
    ]
    assert calls, f"{module} no longer emits an egress guard"
    for call in calls:
        assert len(call.args) == 1, f"{module} calls egress_guard_compose() with no hosts"
        arg = call.args[0]
        assert isinstance(arg, ast.Call) and getattr(arg.func, "id", "") == "fix_source_hosts"
