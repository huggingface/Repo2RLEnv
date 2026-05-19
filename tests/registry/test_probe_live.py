"""Live probe tests against real registries.

These are gated on environment variables and are SKIPPED in normal CI.
Run manually:

    R2E_LIVE_PROBE_LOCAL=1 pytest tests/registry/test_probe_live.py -k local -v
    R2E_LIVE_PROBE_GHCR=1 pytest tests/registry/test_probe_live.py -k ghcr -v

The `local` test spins up a `registry:2` container if `docker` is available;
the `ghcr` test requires a working `gh auth` login with write:packages scope.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time

import pytest

from repo2rlenv.registry.auth import CredentialSource, RegistryAuth, RegistryKind
from repo2rlenv.registry.probe import probe


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.mark.skipif(
    not os.environ.get("R2E_LIVE_PROBE_LOCAL"),
    reason="set R2E_LIVE_PROBE_LOCAL=1 to run; spins up a local registry:2 container",
)
@pytest.mark.skipif(not _docker_available(), reason="docker CLI not available")
class TestProbeLocalRegistry:
    """End-to-end probe against a real `registry:2` container, no auth."""

    @pytest.fixture(autouse=True)
    def _registry_container(self) -> None:
        # Start container
        port = 5555  # unusual port to avoid collisions
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                "r2e-probe-test-registry",
                "-p",
                f"{port}:5000",
                "registry:2",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        for _ in range(30):
            if _port_open("127.0.0.1", port):
                break
            time.sleep(0.5)
        else:  # pragma: no cover
            pytest.fail("local registry never came up")
        yield
        subprocess.run(
            ["docker", "stop", "r2e-probe-test-registry"],
            check=False,
            capture_output=True,
            timeout=30,
        )

    def test_all_levels_pass(self) -> None:
        host = "127.0.0.1:5555"
        auth = RegistryAuth(host, RegistryKind.LOCAL, CredentialSource.EMPTY)
        result = probe(auth, "r2e-test")
        assert result.is_pushable, result


@pytest.mark.skipif(
    not os.environ.get("R2E_LIVE_PROBE_GHCR"),
    reason="set R2E_LIVE_PROBE_GHCR=1 to run; requires gh auth + docker login ghcr.io",
)
class TestProbeGHCR:
    """End-to-end probe against ghcr.io with the user's actual credentials."""

    def test_probe_resolves_correctly(self) -> None:
        # Caller is expected to have `docker login ghcr.io` already done;
        # we just exercise the probe against the live endpoint.
        from repo2rlenv.registry.auth import discover_logged_in_registries

        ghcr = next(
            (a for a in discover_logged_in_registries() if a.kind is RegistryKind.GHCR),
            None,
        )
        if ghcr is None:
            pytest.skip("no ghcr.io login in ~/.docker/config.json")

        # Use the gh CLI username as the namespace (works for any GHCR user).
        proc = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            pytest.skip("gh CLI not available")
        ns = proc.stdout.strip().lower()

        result = probe(ghcr, ns)
        assert result.reachable, result
        assert result.authenticated, result
        # can_read / can_write depend on user's package perms — log only
        print(f"\nGHCR probe for {ns}: {result}")
