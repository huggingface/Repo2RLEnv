from __future__ import annotations

import pytest
import yaml

pytest.importorskip("harbor")

from harbor.models.task.config import EnvironmentConfig, NetworkPolicy
from harbor.models.trial.paths import TrialPaths

from repo2rlenv.execution.harbor_offline import OfflineDockerEnvironment


@pytest.mark.parametrize(
    ("startup", "phase"),
    [("no-network", "no-network"), ("public", "no-network"), ("no-network", "public")],
)
def test_real_harbor_constructor_preserves_offline_contract(tmp_path, monkeypatch, startup, phase):
    monkeypatch.setenv("REPO2RLENV_REMOTE_WORKER", "1")

    def no_local_process(*args, **kwargs):
        pytest.fail("Constructing an offline environment must not probe or start Docker")

    monkeypatch.setattr("subprocess.run", no_local_process)
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir()
    (environment_dir / "Dockerfile").write_text("FROM python:3.12-slim\n")
    options = {
        "environment_dir": environment_dir,
        "environment_name": "offline-contract",
        "session_id": "offline-contract__env",
        "trial_paths": TrialPaths(trial_dir=tmp_path / "trial"),
        "task_env_config": EnvironmentConfig(),
        "network_policy": NetworkPolicy(network_mode=startup),
        "phase_network_policies": [NetworkPolicy(network_mode=phase)],
    }
    if startup != "no-network" or phase != "no-network":
        with pytest.raises(ValueError, match="every execution phase"):
            OfflineDockerEnvironment(**options)
    else:
        environment = OfflineDockerEnvironment(**options)
        assert not environment._enable_egress_control
        overlay = environment._docker_compose_paths[-1]
        assert yaml.safe_load(overlay.read_text())["services"]["main"]["network_mode"] == "none"


def test_untested_harbor_version_is_rejected_before_initialization(monkeypatch):
    monkeypatch.setenv("REPO2RLENV_REMOTE_WORKER", "1")
    monkeypatch.setattr("repo2rlenv.execution.harbor_offline.version", lambda _: "999.0.0")
    with pytest.raises(RuntimeError, match="tested Harbor"):
        OfflineDockerEnvironment()


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
