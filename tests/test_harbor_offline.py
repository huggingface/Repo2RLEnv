from __future__ import annotations

import pytest

pytest.importorskip("harbor")

from harbor.models.task.config import NetworkPolicy

from repo2rlenv.execution.harbor_offline import OfflineDockerEnvironment


def test_adapter_refuses_local_execution_before_touching_docker(monkeypatch):
    monkeypatch.delenv("REPO2RLENV_REMOTE_WORKER", raising=False)
    with pytest.raises(RuntimeError, match="remote worker"):
        OfflineDockerEnvironment()


@pytest.mark.parametrize("mode", ["public", "allowlist"])
def test_offline_policy_never_silently_weakens_a_requested_network_contract(mode):
    environment = object.__new__(OfflineDockerEnvironment)
    with pytest.raises(ValueError, match="every execution phase"):
        environment.validate_network_policy_support(NetworkPolicy(network_mode=mode))
    environment.validate_network_policy_support(NetworkPolicy(network_mode="no-network"))
    assert environment.capabilities.disable_internet
    assert not environment.capabilities.dynamic_network_policy
