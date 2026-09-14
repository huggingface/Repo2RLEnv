from __future__ import annotations

import pytest

from repo2rlenv.execution.base import WorkerSpec
from repo2rlenv.tasksmith.bootstrap_matrix import RepositoryBootstrap, cpu_build, dockerfile
from repo2rlenv.tasksmith.bootstrap_smoke import check
from repo2rlenv.tasksmith.models import Options


def test_bootstrap_never_executes_targets_on_controller(tmp_path, monkeypatch):
    monkeypatch.delenv("REPO2RLENV_REMOTE_WORKER", raising=False)
    spec = RepositoryBootstrap(name="accelerate", ref="a" * 40)
    with pytest.raises(RuntimeError, match="remote only"):
        cpu_build(spec, tmp_path)
    with pytest.raises(RuntimeError, match="remotely only"):
        check("accelerate", "cpu")


def test_snapshot_is_modal_only_and_exclusive_with_registry_image():
    with pytest.raises(ValueError, match="snapshot"):
        WorkerSpec(name="test", provider="daytona", snapshot_id="im-example")
    with pytest.raises(ValueError, match="snapshot"):
        WorkerSpec(name="test", image="ubuntu:24.04", snapshot_id="im-example")
    with pytest.raises(ValueError, match="snapshots"):
        Options(provider="daytona", worker_snapshot="im-example")


def test_gpu_recipe_pins_actual_checkout_without_local_context():
    spec = RepositoryBootstrap(name="peft", ref="a" * 40)
    recipe = dockerfile(spec, "gpu", clone=True)
    assert f"git fetch --depth=1 {spec.url} {spec.ref}" in recipe
    assert 'test "$(git rev-parse HEAD)"' in recipe
    assert "COPY" not in recipe
    assert "HF_HUB_OFFLINE=1" in recipe
    assert "cuda12.8" in recipe
    assert "download.pytorch.org/whl/cpu" not in recipe


def test_reject_non_commit_refs_and_multiline_repairs():
    with pytest.raises(ValueError):
        RepositoryBootstrap(name="peft", ref="main")
    with pytest.raises(ValueError, match="single lines"):
        dockerfile(
            RepositoryBootstrap(name="peft", ref="a" * 40, extra_install=["true\nCOPY . /root"]),
            "cpu",
        )


def test_tokenizers_builds_rust_source():
    recipe = dockerfile(RepositoryBootstrap(name="tokenizers", ref="b" * 40), "cpu")
    assert "sh.rustup.rs" in recipe and "-e ./bindings/python" in recipe


def test_smoke_rejects_installed_package_outside_pinned_checkout(monkeypatch):
    from types import SimpleNamespace

    from repo2rlenv.tasksmith import bootstrap_smoke

    monkeypatch.setenv("REPO2RLENV_REMOTE_WORKER", "1")
    monkeypatch.setattr(
        bootstrap_smoke.importlib,
        "import_module",
        lambda _: SimpleNamespace(__file__="/usr/local/lib/site-packages/peft/__init__.py"),
    )
    with pytest.raises(RuntimeError, match="outside its pinned checkout"):
        check("peft", "cpu")


@pytest.mark.parametrize("error_kind", ["build", "lost_create"])
def test_gpu_failures_settle_known_builds_and_retain_unknown_creates(
    tmp_path, monkeypatch, error_kind
):
    import json
    import sys
    from types import SimpleNamespace

    from repo2rlenv.campaigns.budget import BudgetLedger
    from repo2rlenv.tasksmith.matrix_runner import run_gpu_repository

    class ImageBuildError(Exception):
        pass

    campaign = tmp_path / "campaign"
    ledger = BudgetLedger(campaign / "budget.sqlite3", limit_usd="50")

    def create(*args, **kwargs):
        assert ledger.status()["reserved_usd"] == "6.000000"
        if error_kind == "build":
            raise ImageBuildError("fixture build failed before sandbox creation")
        raise TimeoutError("fixture create response lost")

    monkeypatch.setitem(
        sys.modules,
        "modal",
        SimpleNamespace(
            Image=SimpleNamespace(from_dockerfile=lambda *a, **kw: object()),
            Sandbox=SimpleNamespace(create=create),
            App=SimpleNamespace(lookup=lambda *a, **kw: object()),
            exception=SimpleNamespace(ImageBuildError=ImageBuildError),
        ),
    )
    output = tmp_path / "matrix/gpu/peft"
    result = run_gpu_repository(
        RepositoryBootstrap(name="peft", ref="c" * 40), output, campaign, on_event=lambda _: None
    )
    receipt = json.loads((output / "operation.json").read_text())
    assert result["status"] == "failed"
    if error_kind == "build":
        assert receipt["state"] == "build_failed"
        assert ledger.status()["reserved_usd"] == "0.000000"
        assert float(ledger.status()["accounted_usd"]) >= 1
    else:
        assert receipt["state"] == "creation_uncertain"
        assert ledger.status()["reserved_usd"] == "6.000000"
        assert ledger.status()["accounted_usd"] == "0.000000"
