"""Integration tests for `registry.integration.prepare_dataset_for_push`.

We stub the probe + push primitives and verify:
  - The right mode is selected given (probe results, flags).
  - environment/Dockerfile and task.toml get rewritten in-place.
  - The reproducibility subtable is populated correctly.
  - 1-image-per-dataset enforcement.
  - pr_diff (text-only) datasets skip the image step.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from repo2rlenv.registry import integration as integ
from repo2rlenv.registry.auth import (
    RegistryKind,
)
from repo2rlenv.registry.integration import (
    _bootstrap_image_refs,
    _list_task_dirs,
    _parse_local_tag,
    _rewrite_dockerfile_from,
    prepare_dataset_for_push,
)
from repo2rlenv.registry.probe import ProbeResult


def _write_runtime_task(
    root: Path,
    name: str,
    *,
    bootstrap_ref: str,
    pipeline: str = "pr_runtime",
    repo: str = "pallets/click",
) -> Path:
    task_dir = root / name
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text(
        f"""version = "1.0"

[task]
name = "{name}"
org = "test"
description = "test task"

[metadata.repo2env]
spec_version = "0.1.0"
pipeline = "{pipeline}"
repo = "{repo}"
ref = "abc123"

[metadata.repo2env.{pipeline}]
bootstrap_image = "{bootstrap_ref}"
""",
        encoding="utf-8",
    )
    (task_dir / "instruction.md").write_text("do it", encoding="utf-8")
    sol = task_dir / "solution"
    sol.mkdir()
    (sol / "patch.diff").write_text("", encoding="utf-8")
    env = task_dir / "environment"
    env.mkdir()
    (env / "Dockerfile").write_text(
        f"FROM {bootstrap_ref}\nWORKDIR /workspace\nRUN echo hi\n",
        encoding="utf-8",
    )
    tests = task_dir / "tests"
    tests.mkdir()
    (tests / "test.sh").write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
    return task_dir


def _write_text_only_task(root: Path, name: str) -> Path:
    """pr_diff-shape task — no environment/ dir."""
    task_dir = root / name
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text(
        f"""version = "1.0"

[task]
name = "{name}"
org = "test"
description = "test task"

[metadata.repo2env]
spec_version = "0.1.0"
pipeline = "pr_diff"
repo = "pallets/click"
""",
        encoding="utf-8",
    )
    (task_dir / "instruction.md").write_text("do it", encoding="utf-8")
    sol = task_dir / "solution"
    sol.mkdir()
    (sol / "patch.diff").write_text("--- a/x\n+++ b/x\n", encoding="utf-8")
    return task_dir


def _make_pushable_probe(
    host: str = "ghcr.io", kind: RegistryKind = RegistryKind.GHCR
) -> ProbeResult:
    return ProbeResult(
        host=host,
        kind=kind,
        namespace="testorg",
        levels_checked=(1, 2, 3, 4),
        reachable=True,
        authenticated=True,
        can_read=True,
        can_write=True,
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class TestHelpers:
    def test_parse_local_tag(self) -> None:
        owner, name, sha = _parse_local_tag("local/r2e-bootstrap/pallets__click:a1b2c3d4e5f6")
        assert owner == "pallets"
        assert name == "click"
        assert sha == "a1b2c3d4e5f6"

    def test_parse_local_tag_with_opts(self) -> None:
        _owner, _name, sha = _parse_local_tag(
            "local/r2e-bootstrap/pallets__click:a1b2c3d4e5f6__01234567"
        )
        assert sha == "a1b2c3d4e5f6"

    def test_parse_local_tag_digest_form(self) -> None:
        """Real-world inputs use the @sha256:... digest form, not :tag."""
        owner, name, sha = _parse_local_tag(
            "local/r2e-bootstrap/pallets__click@sha256:"
            "be4ae3decdbef39ead00d4f5dde6f0dbb644666d82bfa0693ae066e1cc9b467a"
        )
        assert owner == "pallets"
        assert name == "click"
        assert sha == "be4ae3decdbe"
        assert len(sha) == 12  # truncated for OCI tag-length safety

    def test_rewrite_dockerfile_from(self, tmp_path: Path) -> None:
        df = tmp_path / "Dockerfile"
        df.write_text("FROM local/foo:tag\nWORKDIR /x\n", encoding="utf-8")
        changed = _rewrite_dockerfile_from(df, "ghcr.io/u/n@sha256:abc")
        assert changed
        assert df.read_text().startswith("FROM ghcr.io/u/n@sha256:abc")

    def test_list_task_dirs_skips_hidden(self, tmp_path: Path) -> None:
        _write_runtime_task(tmp_path, "task-1", bootstrap_ref="local/x:1")
        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / "task.toml").write_text("ignored")
        out = _list_task_dirs(tmp_path)
        assert len(out) == 1
        assert out[0].name == "task-1"

    def test_bootstrap_image_refs(self, tmp_path: Path) -> None:
        _write_runtime_task(tmp_path, "task-1", bootstrap_ref="local/r2e:1")
        _write_runtime_task(tmp_path, "task-2", bootstrap_ref="local/r2e:1")
        refs = _bootstrap_image_refs(_list_task_dirs(tmp_path))
        assert {r for r, _ in refs} == {"local/r2e:1"}


# --------------------------------------------------------------------------
# Mode selection
# --------------------------------------------------------------------------


class TestModeSelection:
    """The big one: given probe + flag combinations, the right mode wins."""

    def test_text_only_dataset_skipped(self, tmp_path: Path) -> None:
        _write_text_only_task(tmp_path, "task-1")
        _write_text_only_task(tmp_path, "task-2")
        result = prepare_dataset_for_push(tmp_path, hf_owner="testorg")
        assert result.mode == "local_only"
        assert result.tasks_rewritten == 0

    def test_self_contained_public_base_takes_fast_path(self, tmp_path: Path) -> None:
        """pr_diff-shape Dockerfiles (FROM python:3.12-slim) skip image push
        AND get their reproducibility metadata rewritten to inline_dockerfile
        + public."""
        _write_runtime_task(tmp_path, "task-1", bootstrap_ref="python:3.12-slim")
        _write_runtime_task(tmp_path, "task-2", bootstrap_ref="python:3.12-slim")
        result = prepare_dataset_for_push(tmp_path, hf_owner="testorg")
        assert result.mode == "inline_dockerfile"
        assert result.tasks_rewritten == 2
        assert result.image_visibility == "public"
        assert result.inline_recipe_source == "user_dockerfile"
        # Every task.toml's reproducibility must reflect the new mode.
        for td in (tmp_path / "task-1", tmp_path / "task-2"):
            data = tomllib.loads((td / "task.toml").read_text())
            repro = data["metadata"]["repo2env"]["reproducibility"]
            assert repro["mode"] == "inline_dockerfile"
            assert repro["image_visibility"] == "public"
            assert repro["image_ref"] == "python:3.12-slim"
            assert repro["inline_recipe_source"] == "user_dockerfile"
            assert repro["inline_recipe_sha256"].startswith("sha256:")
            assert repro["inline_recipe_lines"] > 0

    def test_custom_unqualified_image_not_self_contained(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unqualified image that isn't on the public-base allowlist must
        NOT take the fast path. It looks "not local" by the legacy check,
        but the user might have meant a private registry or a hand-built
        image — silently skipping the push would publish a broken dataset.
        Fall through to the normal registry/inline-Dockerfile path instead;
        when no probe + no cached Dockerfile exist we surface a clear error
        instead of silently returning ``mode=local_only``."""
        _write_runtime_task(tmp_path, "task-1", bootstrap_ref="my-bootstrap:latest")
        monkeypatch.setattr(
            integ,
            "_select_verified_registry",
            lambda *a, **kw: (None, None, []),
        )
        # No registry probe + inline path can't find the cached Dockerfile
        # for `my-bootstrap:latest` → raises, which is the desired surfaced
        # failure (vs the old fast path silently returning local_only).
        with pytest.raises(RuntimeError, match="inline mode requires"):
            prepare_dataset_for_push(tmp_path, hf_owner="testorg")

    def test_multi_image_dataset_fails_fast(self, tmp_path: Path) -> None:
        _write_runtime_task(tmp_path, "task-1", bootstrap_ref="local/r2e:img1")
        _write_runtime_task(tmp_path, "task-2", bootstrap_ref="local/r2e:img2")
        with pytest.raises(RuntimeError, match="distinct bootstrap images"):
            prepare_dataset_for_push(tmp_path, hf_owner="testorg")

    def test_registry_mode_selected_when_probe_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_runtime_task(tmp_path, "task-1", bootstrap_ref="local/r2e:abc123def456")

        # Stub selection to return a pushable GHCR probe
        probe = _make_pushable_probe()

        def fake_select(hf_owner: str, **kwargs: Any) -> Any:
            return (probe, "testorg", [probe])

        monkeypatch.setattr(integ, "_select_verified_registry", fake_select)
        # Stub the push to succeed
        monkeypatch.setattr(
            integ,
            "_do_push",
            lambda local, remote, looks_local: mock.MagicMock(
                digest=f"{remote.rsplit(':', 1)[0]}@sha256:cafe",
                pushed=True,
            ),
        )
        # Stub visibility flip
        monkeypatch.setattr(
            integ,
            "ensure_ghcr_visibility",
            lambda ref, target="public": mock.MagicMock(success=True, error=None, manual_url=None),
        )

        result = prepare_dataset_for_push(tmp_path, hf_owner="testorg")
        assert result.mode == "registry"
        assert result.tasks_rewritten == 1
        assert result.image_digest and result.image_digest.endswith("@sha256:cafe")
        # Verify Dockerfile got rewritten
        df = tmp_path / "task-1" / "environment" / "Dockerfile"
        assert "@sha256:cafe" in df.read_text()
        # Verify task.toml carries the reproducibility subtable
        toml_data = tomllib.loads((tmp_path / "task-1" / "task.toml").read_text())
        repro = toml_data["metadata"]["repo2env"]["reproducibility"]
        assert repro["mode"] == "registry"
        assert "cafe" in repro["image_ref"]
        assert toml_data["metadata"]["repo2env"]["spec_version"] == "0.2.0"
        # Legacy field also updated
        assert (
            toml_data["metadata"]["repo2env"]["pr_runtime"]["bootstrap_image"] == repro["image_ref"]
        )

    def test_auto_fallback_to_inline_when_no_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_runtime_task(
            tmp_path, "task-1", bootstrap_ref="local/r2e-bootstrap/pallets__click:abc123def456"
        )

        # No registry available
        monkeypatch.setattr(
            integ, "_select_verified_registry", lambda *a, **kw: (None, "testorg", [])
        )

        # Provide a fake bootstrap recipe via the helper
        monkeypatch.setattr(
            integ,
            "_load_bootstrap_recipe",
            lambda local_ref: (
                "FROM python:3.11-slim\nRUN apt-get install -y git\n",
                "agent_replay",
            ),
        )

        result = prepare_dataset_for_push(tmp_path, hf_owner="testorg")
        assert result.mode == "inline_dockerfile"
        assert result.tasks_rewritten == 1
        assert result.fallback_reason is not None
        assert "registry credentials" in (result.fallback_reason or "")
        # The Dockerfile should now contain the recipe
        df = tmp_path / "task-1" / "environment" / "Dockerfile"
        content = df.read_text()
        assert "python:3.11-slim" in content
        assert "apt-get install -y git" in content
        # And the per-task overlay should still be there (WORKDIR / RUN echo hi)
        assert "WORKDIR /workspace" in content
        # task.toml reflects inline mode
        toml_data = tomllib.loads((tmp_path / "task-1" / "task.toml").read_text())
        repro = toml_data["metadata"]["repo2env"]["reproducibility"]
        assert repro["mode"] == "inline_dockerfile"
        assert repro["inline_recipe_source"] == "agent_replay"
        assert repro["fallback_reason"]

    def test_require_registry_hard_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_runtime_task(tmp_path, "task-1", bootstrap_ref="local/r2e:abc123def456")
        monkeypatch.setattr(
            integ, "_select_verified_registry", lambda *a, **kw: (None, "testorg", [])
        )
        with pytest.raises(RuntimeError, match="no verified registry"):
            prepare_dataset_for_push(tmp_path, hf_owner="testorg", require_registry=True)

    def test_explicit_inline_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_runtime_task(tmp_path, "task-1", bootstrap_ref="local/r2e:abc123def456")
        monkeypatch.setattr(
            integ,
            "_load_bootstrap_recipe",
            lambda local_ref: ("FROM python:3.11-slim\n", "user_dockerfile"),
        )
        # Even if a registry IS available, --inline-dockerfile wins
        result = prepare_dataset_for_push(tmp_path, hf_owner="testorg", inline_dockerfile=True)
        assert result.mode == "inline_dockerfile"
        assert result.inline_recipe_source == "user_dockerfile"
        # No fallback reason — this was explicit
        assert result.fallback_reason is None

    def test_inline_fails_without_cached_recipe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_runtime_task(tmp_path, "task-1", bootstrap_ref="local/r2e:abc123def456")
        monkeypatch.setattr(integ, "_load_bootstrap_recipe", lambda lr: (None, None))
        with pytest.raises(RuntimeError, match="bootstrap reconstructed Dockerfile"):
            prepare_dataset_for_push(tmp_path, hf_owner="testorg", inline_dockerfile=True)

    def test_visibility_flip_failure_in_default_mode_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_runtime_task(tmp_path, "task-1", bootstrap_ref="local/r2e:abc123def456")
        probe = _make_pushable_probe()
        monkeypatch.setattr(
            integ, "_select_verified_registry", lambda *a, **kw: (probe, "testorg", [probe])
        )
        monkeypatch.setattr(
            integ,
            "_do_push",
            lambda local, remote, looks_local: mock.MagicMock(
                digest=f"{remote.rsplit(':', 1)[0]}@sha256:cafe",
                pushed=True,
            ),
        )
        # Visibility flip fails
        monkeypatch.setattr(
            integ,
            "ensure_ghcr_visibility",
            lambda ref, target="public": mock.MagicMock(
                success=False,
                error="not enough perms",
                manual_url="https://github.com/users/testorg/packages/...",
            ),
        )

        result = prepare_dataset_for_push(tmp_path, hf_owner="testorg")
        # Dataset still publishes (registry mode), but with visibility=unknown + warning
        assert result.mode == "registry"
        assert result.image_visibility == "unknown"
        assert result.warnings
        assert "visibility" in result.warnings[0].lower()

    def test_visibility_flip_failure_with_require_registry_hard_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_runtime_task(tmp_path, "task-1", bootstrap_ref="local/r2e:abc123def456")
        probe = _make_pushable_probe()
        monkeypatch.setattr(
            integ, "_select_verified_registry", lambda *a, **kw: (probe, "testorg", [probe])
        )
        monkeypatch.setattr(
            integ,
            "_do_push",
            lambda local, remote, looks_local: mock.MagicMock(
                digest=f"{remote.rsplit(':', 1)[0]}@sha256:cafe",
                pushed=True,
            ),
        )
        monkeypatch.setattr(
            integ,
            "ensure_ghcr_visibility",
            lambda ref, target="public": mock.MagicMock(success=False, error="x", manual_url="y"),
        )
        with pytest.raises(RuntimeError, match="visibility"):
            prepare_dataset_for_push(tmp_path, hf_owner="testorg", require_registry=True)
