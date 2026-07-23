"""Anti-contamination env-guard helpers (egress denylist + git-history scrub)."""

from __future__ import annotations

from repo2rlenv.pipelines._env_guard import (
    DEFAULT_ALLOW_HOSTS,
    FIX_SOURCE_HOSTS,
    egress_firewall_compose,
    egress_firewall_dockerfile_fragment,
    egress_firewall_entrypoint,
    egress_guard_compose,
    git_history_scrub,
)


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


def test_v2_firewall_allowlist_includes_agent_installer_hosts():
    # Regression: v2 iptables OUTPUT DROP was too aggressive on first roll-out.
    # `apt-get install curl` (agent adapter bootstrap) failed because
    # `deb.debian.org` wasn't in the allow-list — every claude-code/codex/
    # openhands-sdk track exited with NonZeroAgentExitCodeError (exit 100).
    # The allow-list must include debian mirrors + pypi + node so the agent
    # can install its own dependencies. Fix-content sources (raw.github,
    # pypi wheels of the target repo) are still refused at DNS-blackhole layer
    # or blocked by not-listing github.com itself.
    required = (
        "deb.debian.org",
        "security.debian.org",
        "pypi.org",
        "files.pythonhosted.org",
    )
    for host in required:
        assert host in DEFAULT_ALLOW_HOSTS, f"{host} missing from v2 allow-list"


def test_v2_firewall_entrypoint_uses_allowlist():
    script = egress_firewall_entrypoint()
    # Loopback + established connections must stay ACCEPT so the container
    # can reach itself and receive responses to outbound requests.
    assert "iptables -A OUTPUT -o lo -j ACCEPT" in script
    assert "state --state RELATED,ESTABLISHED" in script
    # Final policy is DROP.
    assert "iptables -P OUTPUT DROP" in script
    # Every host in the allow-list appears verbatim in the entrypoint.
    for host in DEFAULT_ALLOW_HOSTS:
        assert host in script, f"host {host!r} missing from firewall script"


def test_v2_firewall_dockerfile_installs_iptables():
    fragment = egress_firewall_dockerfile_fragment()
    # Multi-package-manager fallback for iptables install.
    assert "iptables" in fragment
    assert "apt-get" in fragment or "apk" in fragment
    # Delivers the entrypoint script via chmoded heredoc.
    assert "EGRESS_EOF" in fragment


def test_v2_firewall_compose_declares_cap_add_net_admin():
    compose = egress_firewall_compose()
    # CAP_NET_ADMIN is required so the entrypoint can install iptables rules.
    assert "cap_add" in compose
    assert "NET_ADMIN" in compose
    # The compose command must invoke the firewall entrypoint.
    assert "/entrypoint-egress.sh" in compose


def test_build_environment_dockerfile_includes_scrub_and_keeps_base():
    from repo2rlenv.pipelines.pr_runtime import build_environment_dockerfile

    df = build_environment_dockerfile("local/r2e-bootstrap/x:abc", "9d2747057c4a")
    assert "git reset --hard 9d2747057c4a" in df  # working tree at base
    assert "git remote remove origin" in df  # future pruned
    assert "git gc --prune=now" in df
