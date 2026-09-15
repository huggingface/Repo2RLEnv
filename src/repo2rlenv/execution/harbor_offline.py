"""Harbor 0.20.0 adapter for permanently offline, single-container tasks.

Modal's VM kernel lacks nft_fib support required by Harbor's dynamic firewall.
These recipes need no runtime network transitions: Docker's isolated network
namespace provides the narrower contract directly, including UDP and ICMP.
"""

from __future__ import annotations

import os
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

from harbor.environments.capabilities import EnvironmentCapabilities
from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.task.config import NetworkMode, NetworkPolicy


class OfflineDockerEnvironment(DockerEnvironment):
    def __init__(self, *args, **kwargs):
        if os.environ.get("REPO2RLENV_REMOTE_WORKER") != "1":
            raise RuntimeError("The owned Docker adapter runs inside a remote worker only")
        if version("harbor") != "0.20.0":
            raise RuntimeError("Offline adapter requires the tested Harbor 0.20.0 contract")
        super().__init__(*args, **kwargs)
        if (
            self._is_windows_container
            or self._environment_docker_compose_path.exists()
            or self.extra_docker_compose_paths
        ):
            raise ValueError(
                "Offline adapter supports Linux Dockerfile tasks without extra services"
            )

    @staticmethod
    def _requires_egress_control(*, startup_network_policy, phase_network_policies):
        # Policy validation below rejects every mode requiring a sidecar. The
        # namespace policy is added last to the generated compose configuration.
        return False

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(disable_internet=True, mounted=True)

    def validate_network_policy_support(self, network_policy: NetworkPolicy | None = None) -> None:
        policy = network_policy or self.network_policy
        if policy.network_mode != NetworkMode.NO_NETWORK or policy.allowed_hosts:
            raise ValueError("Offline adapter requires no-network in every execution phase")

    @property
    def _docker_compose_paths(self) -> list[Path]:
        return [
            *super()._docker_compose_paths,
            Path(str(files(__package__).joinpath("offline-compose.yaml"))),
        ]
