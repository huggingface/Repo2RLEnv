"""Anti-contamination env-guard helpers (egress denylist + git-history scrub)."""

from __future__ import annotations

import ast
import importlib
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
