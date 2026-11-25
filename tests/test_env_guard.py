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

from repo2rlenv.bootstrap.spec import LanguageHint
from repo2rlenv.pipelines._env_guard import (
    FIX_SOURCE_HOSTS,
    GITLAB_FIX_SOURCE_HOSTS,
    GO_FIX_SOURCE_HOSTS,
    RUST_FIX_SOURCE_HOSTS,
    egress_guard_compose,
    fix_source_hosts,
    git_history_scrub,
    offline_env,
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


def _history_repo(tmp_path, monkeypatch, clone_args):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "t")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "t@example.com")
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "uploadpack.allowFilter", "true")
    (upstream / "README").write_text("An ancestor of the task base.\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-qm", "ancestor")
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
    return workspace, base, fix


def _scrub(workspace, base):
    for run in re.split(r"^RUN ", git_history_scrub(base), flags=re.MULTILINE):
        step = run.replace("\\\n", " ").strip()
        if step:
            subprocess.run(
                ["sh", "-c", step], cwd=workspace, capture_output=True, text=True, check=True
            )


@pytest.mark.skipif(shutil.which("git") is None or os.name == "nt", reason="needs git and sh")
@pytest.mark.parametrize("clone_args", [["--filter=blob:none"], []], ids=["partial", "full"])
def test_git_history_scrub_leaves_no_later_objects(tmp_path, monkeypatch, clone_args):
    """Run the emitted RUN lines; the base remains usable and the fix is absent."""
    workspace, base, fix = _history_repo(tmp_path, monkeypatch, clone_args)
    _scrub(workspace, base)

    assert _git(workspace, "cat-file", "-e", fix).returncode != 0
    objects = subprocess.run(
        ["git", "cat-file", "--batch-all-objects", "--batch"], cwd=workspace, capture_output=True
    )
    assert b"the fix" not in objects.stdout
    commits = _git(workspace, "cat-file", "--batch-all-objects", "--batch-check")
    stored_commits = {line.split()[0] for line in commits.stdout.splitlines() if " commit " in line}
    assert stored_commits == set(_git(workspace, "rev-list", base).stdout.splitlines())
    # The verifier still resets files against base and diffs against it.
    assert _git(workspace, "checkout", base, "--", "lib.py").returncode == 0
    assert _git(workspace, "diff", "--quiet", base).returncode == 0
    assert _git(workspace, "fsck", "--no-progress").returncode == 0


@pytest.mark.skipif(shutil.which("git") is None or os.name == "nt", reason="needs git and sh")
@pytest.mark.parametrize("leftover", ["ref", "remote", "kept-pack"])
def test_git_history_scrub_rejects_incomplete_cleanup(tmp_path, monkeypatch, leftover):
    workspace, base, fix = _history_repo(tmp_path, monkeypatch, [])
    if leftover == "ref":
        assert _git(workspace, "update-ref", "refs/pull/1/head", fix).returncode == 0
    elif leftover == "remote":
        assert (
            _git(workspace, "remote", "add", "backup", str(tmp_path / "upstream")).returncode == 0
        )
    else:
        # A kept pack survives a successful gc even when all future refs vanish.
        packs = list((workspace / ".git/objects/pack").glob("*.pack"))
        assert packs
        for pack in packs:
            pack.with_suffix(".keep").touch()
    with pytest.raises(subprocess.CalledProcessError) as failure:
        _scrub(workspace, base)
    assert "Git history scrub verification failed" in failure.value.stderr


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


def _is_bootstrap_language(node: ast.expr) -> bool:
    """True for the AST of `self.bootstrap.language`."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "language"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "bootstrap"
    )


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
        # The guard takes (hosts, language): a task that drops the language
        # silently loses its Go/Rust registry block.
        assert len(call.args) == 2, f"{module} calls egress_guard_compose() without a language"
        hosts_arg, language_arg = call.args
        assert isinstance(hosts_arg, ast.Call)
        assert getattr(hosts_arg.func, "id", "") == "fix_source_hosts"
        assert len(hosts_arg.args) == 2, f"{module} picks hosts without the task's language"
        assert _is_bootstrap_language(hosts_arg.args[1]), f"{module} hosts arg is not the language"
        assert _is_bootstrap_language(language_arg), f"{module} overlay env is not the language"


# --- per-language registries (Go / Rust) --------------------------------------


def test_go_blackholes_the_module_proxy():
    hosts = fix_source_hosts(SourceKind.GITHUB, LanguageHint.GO)
    # The module proxy mirrors the repo: leaving it up closes nothing for Go.
    assert set(GO_FIX_SOURCE_HOSTS).issubset(hosts)
    assert set(FIX_SOURCE_HOSTS).issubset(hosts)


def test_rust_blackholes_the_crate_registries():
    hosts = fix_source_hosts(SourceKind.GITHUB, LanguageHint.RUST)
    assert set(RUST_FIX_SOURCE_HOSTS).issubset(hosts)
    assert set(FIX_SOURCE_HOSTS).issubset(hosts)


@pytest.mark.parametrize(
    "language",
    [
        LanguageHint.PYTHON,
        LanguageHint.NODE,
        LanguageHint.JAVA,
        LanguageHint.C_CPP,
        LanguageHint.UNKNOWN,
        None,
    ],
)
def test_other_languages_keep_the_base_list(language):
    """A non-Go/Rust task's host list and overlay must not change."""
    assert fix_source_hosts(SourceKind.GITHUB, language) == FIX_SOURCE_HOSTS
    assert offline_env(language) == {}
    assert "environment:" not in egress_guard_compose(fix_source_hosts(None, language), language)


def test_language_may_be_passed_as_a_plain_string():
    """Callers hold either the enum or its value; both must work."""
    assert fix_source_hosts(None, "go") == fix_source_hosts(None, LanguageHint.GO)
    assert fix_source_hosts(None, "RUST") == fix_source_hosts(None, LanguageHint.RUST)


def test_gitlab_and_go_compose_together():
    hosts = fix_source_hosts(SourceKind.GITLAB, LanguageHint.GO)
    assert "gitlab.com" in hosts and "proxy.golang.org" in hosts


def test_go_overlay_sets_goproxy_off():
    compose = egress_guard_compose(fix_source_hosts(None, LanguageHint.GO), LanguageHint.GO)
    assert 'GOPROXY: "off"' in compose
    # A cache miss must not go looking for a checksum either.
    assert 'GOSUMDB: "off"' in compose


def test_rust_overlay_sets_cargo_offline():
    compose = egress_guard_compose(fix_source_hosts(None, LanguageHint.RUST), LanguageHint.RUST)
    assert 'CARGO_NET_OFFLINE: "true"' in compose


def test_go_overlay_preserves_vendoring():
    """`GOFLAGS=-mod=mod` would drop vendor/ and force a proxy fetch."""
    compose = egress_guard_compose(fix_source_hosts(None, LanguageHint.GO), LanguageHint.GO)
    assert "GOFLAGS" not in compose


@pytest.mark.parametrize("language", [LanguageHint.GO, LanguageHint.RUST])
def test_language_overlay_is_valid_yaml(language):
    import yaml  # pyyaml is a transitive dep; skip cleanly if absent

    hosts = fix_source_hosts(None, language)
    service = yaml.safe_load(egress_guard_compose(hosts, language))["services"]["main"]
    assert [f"{h}:0.0.0.0" for h in hosts] == service["extra_hosts"]
    assert service["environment"] == offline_env(language)


def test_language_overlay_keeps_the_agent_installable():
    """The agent fetches itself at run time; its registries must stay up."""
    for language in (LanguageHint.GO, LanguageHint.RUST):
        compose = egress_guard_compose(fix_source_hosts(None, language), language)
        for allowed in ("registry.npmjs.org", "api.anthropic.com", "deb.debian.org"):
            assert allowed not in compose
