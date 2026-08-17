"""Unit tests for `pipelines/_env_setup_artifacts.py` — gate 0's shipped files.

These are shipped executables; their exit codes are the contract, so the
probes are run as real subprocesses against real temp directories, not
mocked. `importlib.metadata` is exercised for real via a fabricated
dist-info directory placed on `PYTHONPATH`.

A subset of assertions require the package under test to resolve from a
path that literally starts with `/workspace` (the probes hardcode that
absolute prefix — see RFC 0008 §7c/§7d). This dev machine's root filesystem
is a sealed, read-only APFS volume (no `sudo` available either), so we
cannot create a real `/workspace` directory outside a container. Docker is
available in this environment, so those specific assertions run inside a
throwaway `python:3.12-slim` / `node:22-slim` container with a temp dir
bind-mounted at `/workspace`, and are skipped cleanly (not weakened) when
`docker` is unavailable. Every other assertion — which does not require a
literal `/workspace` to exist — runs as a plain local subprocess.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from repo2rlenv.bootstrap.docker import is_docker_available
from repo2rlenv.pipelines._env_setup_artifacts import (
    provenance_js_source,
    provenance_probe_files,
    provenance_py_source,
    provenance_read_py_source,
    provenance_run_sh_source,
)

_DOCKER = is_docker_available()
_NODE = shutil.which("node") is not None

pytestmark_docker = pytest.mark.skipif(
    not _DOCKER,
    reason="docker required to create a real /workspace mount for this assertion",
)


def test_docker_marker_requires_a_live_daemon():
    """The gate must track daemon availability, not just the binary on PATH.

    `shutil.which("docker")` is true on any dev box with Docker Desktop
    installed but stopped, so these container tests errored instead of
    skipping — and would go red in a CI runner without a daemon.
    """
    import tests.test_env_setup_artifacts as mod
    from repo2rlenv.bootstrap.docker import is_docker_available

    assert is_docker_available() == mod._DOCKER


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_probe_dir(tmp_path: Path) -> Path:
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    for name, content in provenance_probe_files().items():
        (probe_dir / name).write_text(content, encoding="utf-8")
    return probe_dir


def _write_dist_info(
    site_dir: Path, dist_name: str, *, direct_url: str | None, version: str = "1.0.0"
) -> None:
    dist_info = site_dir / f"{dist_name}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist_name}\nVersion: {version}\n", encoding="utf-8"
    )
    if direct_url is not None:
        (dist_info / "direct_url.json").write_text(
            json.dumps({"url": direct_url}), encoding="utf-8"
        )


def _write_importable_package(site_dir: Path, import_name: str) -> None:
    pkg = site_dir / import_name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")


def _run_python_probe(
    probe_dir: Path, cfg: dict, rung: str, *, pythonpath: str | None = None
) -> subprocess.CompletedProcess:
    cfg_path = probe_dir / "provenance.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    env = None
    if pythonpath is not None:
        import os

        env = dict(os.environ)
        env["PYTHONPATH"] = pythonpath
    return subprocess.run(
        [sys.executable, str(probe_dir / "provenance.py"), str(cfg_path), rung],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _run_python_read(probe_dir: Path, cfg_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(probe_dir / "provenance_read.py"), str(cfg_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _docker_run(image: str, mounts: list[tuple[str, str, str]], env: dict, args: list[str]):
    cmd = ["docker", "run", "--rm"]
    for host, container, mode in mounts:
        cmd += ["-v", f"{host}:{container}:{mode}"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [image, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


# ---------------------------------------------------------------------------
# accessor sanity
# ---------------------------------------------------------------------------


def test_accessors_return_expected_bodies():
    assert "argv[2]" in provenance_py_source()
    assert "one line each" in provenance_read_py_source()
    assert "node_modules" in provenance_js_source()
    assert 'LANG_ID="$1"' in provenance_run_sh_source()
    files = provenance_probe_files()
    assert set(files) == {
        "provenance_read.py",
        "provenance.py",
        "provenance.js",
        "provenance_run.sh",
    }


def test_provenance_run_sh_has_no_provenance_json_read():
    """RFC 0008 gate-0 rationale: an earlier draft had provenance_run.sh do
    its own inline `python3 -c` read of provenance.json with a *different*
    failure posture than provenance_read.py's — two reads, two postures, one
    script. Gate 0 exists specifically to avoid that; assert it stays gone.
    """
    source = provenance_run_sh_source()
    # The only two mentions of provenance.json must be as an *argument* being
    # passed to provenance.py / provenance.js, never read directly here.
    assert "python3 -c" not in source
    assert "json.load" not in source
    assert source.count("provenance.json") == 2  # one per language branch, both as an argv


# ---------------------------------------------------------------------------
# provenance_read.py contract
# ---------------------------------------------------------------------------


def test_provenance_read_contract(tmp_path):
    probe_dir = _write_probe_dir(tmp_path)

    good = tmp_path / "good.json"
    good.write_text(
        json.dumps({"probe": "direct_url", "base_commit": "abc123", "language": "python"}),
        encoding="utf-8",
    )
    result = _run_python_read(probe_dir, good)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "direct_url\nabc123\npython\n"
    lines = result.stdout.splitlines()
    assert lines == ["direct_url", "abc123", "python"]

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not json", encoding="utf-8")
    assert _run_python_read(probe_dir, malformed).returncode != 0

    missing_key = tmp_path / "missing_key.json"
    missing_key.write_text(
        json.dumps({"probe": "direct_url", "language": "python"}), encoding="utf-8"
    )
    assert _run_python_read(probe_dir, missing_key).returncode != 0

    blank_value = tmp_path / "blank.json"
    blank_value.write_text(
        json.dumps({"probe": "  ", "base_commit": "abc123", "language": "python"}),
        encoding="utf-8",
    )
    assert _run_python_read(probe_dir, blank_value).returncode != 0

    newline_value = tmp_path / "newline.json"
    newline_value.write_text(
        json.dumps({"probe": "direct_url", "base_commit": "abc\n123", "language": "python"}),
        encoding="utf-8",
    )
    assert _run_python_read(probe_dir, newline_value).returncode != 0


# ---------------------------------------------------------------------------
# provenance.py — the Python probe
# ---------------------------------------------------------------------------


def test_provenance_probe_python_direct_url(tmp_path):
    probe_dir = _write_probe_dir(tmp_path)

    # (1) direct_url.json pointing at file:///workspace -> passes, even
    # though the import itself resolves nowhere real (the OR short-circuits
    # on the dist-metadata check; the import name need not even exist).
    site_a = tmp_path / "site_a"
    site_a.mkdir()
    _write_dist_info(site_a, "widget", direct_url="file:///workspace/widget")
    cfg_a = {
        "probe": "direct_url",
        "base_commit": "x",
        "language": "python",
        "package": "widget_pkg_that_does_not_exist",
        "dist_name": "widget",
    }
    result_a = _run_python_probe(probe_dir, cfg_a, "direct_url", pythonpath=str(site_a))
    assert result_a.returncode == 0, result_a.stderr

    # (3) fails: site-packages import with no direct_url.json.
    site_c = tmp_path / "lib" / "python3.12" / "site-packages"
    site_c.mkdir(parents=True)
    _write_importable_package(site_c, "widgetpkg")
    _write_dist_info(site_c, "widget", direct_url=None)
    cfg_c = {
        "probe": "direct_url",
        "base_commit": "x",
        "language": "python",
        "package": "widgetpkg",
        "dist_name": "widget",
    }
    result_c = _run_python_probe(probe_dir, cfg_c, "direct_url", pythonpath=str(site_c))
    assert result_c.returncode != 0

    # (4) a missing dist_name key does not raise — clean sys.exit(1), no
    # traceback, when import also fails to resolve under /workspace.
    cfg_d = {
        "probe": "direct_url",
        "base_commit": "x",
        "language": "python",
        "package": "widgetpkg",
        # no "dist_name" key at all
    }
    result_d = _run_python_probe(probe_dir, cfg_d, "direct_url", pythonpath=str(site_c))
    assert result_d.returncode != 0
    assert "Traceback" not in result_d.stderr
    assert "KeyError" not in result_d.stderr


@pytestmark_docker
def test_provenance_probe_python_direct_url_import_under_real_workspace(tmp_path):
    """(2) passes for import-path-under-/workspace with no dist metadata.

    Requires a literal /workspace; see module docstring for why this needs
    Docker on this machine.
    """
    ws = tmp_path / "workspace"
    _write_importable_package(ws, "widgetpkg")
    probe_dir = _write_probe_dir(tmp_path)
    cfg = {
        "probe": "direct_url",
        "base_commit": "x",
        "language": "python",
        "package": "widgetpkg",
        # no dist_name -> from_workspace_dist("") is a clean False, falls to import check
    }
    (probe_dir / "provenance.json").write_text(json.dumps(cfg), encoding="utf-8")

    result = _docker_run(
        "python:3.12-slim",
        [(str(ws), "/workspace", "ro"), (str(probe_dir), "/probe", "ro")],
        {"PYTHONPATH": "/workspace"},
        ["python3", "/probe/provenance.py", "/probe/provenance.json", "direct_url"],
    )
    assert result.returncode == 0, result.stderr


def test_provenance_probe_path_rung_ignores_dist_metadata(tmp_path):
    """With probe="path", a dist carrying direct_url.json but importing from
    site-packages FAILS, where the same input on "direct_url" PASSES. This
    is the guard against the rung being read but ignored — an earlier draft
    ran the OR unconditionally and never read `probe` at all.
    """
    probe_dir = _write_probe_dir(tmp_path)
    site = tmp_path / "lib" / "site-packages"
    site.mkdir(parents=True)
    _write_dist_info(site, "widget", direct_url="file:///workspace/widget")
    _write_importable_package(site, "widgetpkg")

    cfg = {
        "probe": "direct_url",
        "base_commit": "x",
        "language": "python",
        "package": "widgetpkg",
        "dist_name": "widget",
    }

    direct_url_result = _run_python_probe(probe_dir, cfg, "direct_url", pythonpath=str(site))
    assert direct_url_result.returncode == 0, direct_url_result.stderr

    path_result = _run_python_probe(probe_dir, cfg, "path", pythonpath=str(site))
    assert path_result.returncode != 0


def test_probe_degrades_in_one_step_to_none(tmp_path):
    """The degradation for a failing Python probe is direct_url -> none,
    never direct_url -> path. Demonstrated by: a genuine PyPI-shortcut shape
    (dist metadata present, no direct_url.json, import resolves outside
    /workspace) fails on BOTH rungs — there is no intermediate "path" rescue,
    so the only sane degradation is straight to "none".
    """
    probe_dir = _write_probe_dir(tmp_path)
    site = tmp_path / "lib" / "site-packages"
    site.mkdir(parents=True)
    _write_dist_info(site, "widget", direct_url=None)  # PyPI install: no direct_url.json
    _write_importable_package(site, "widgetpkg")

    cfg = {
        "probe": "direct_url",
        "base_commit": "x",
        "language": "python",
        "package": "widgetpkg",
        "dist_name": "widget",
    }

    direct_url_result = _run_python_probe(probe_dir, cfg, "direct_url", pythonpath=str(site))
    assert direct_url_result.returncode != 0

    path_result = _run_python_probe(probe_dir, cfg, "path", pythonpath=str(site))
    assert path_result.returncode != 0  # path does not rescue a direct_url failure


# ---------------------------------------------------------------------------
# provenance.js — the Node probe
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _NODE, reason="node unavailable")
def test_provenance_probe_node_resolution_failure_exits_nonzero(tmp_path):
    """A package that cannot be resolved at all (wrong name, no /workspace
    needed to demonstrate this) fails cleanly. Runs directly with the local
    `node` binary — no Docker needed for this assertion.
    """
    probe_dir = _write_probe_dir(tmp_path)
    cfg_path = probe_dir / "provenance.json"
    cfg_path.write_text(json.dumps({"package": "definitely-does-not-exist-xyz"}), encoding="utf-8")
    result = subprocess.run(
        ["node", str(probe_dir / "provenance.js"), str(cfg_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0


@pytestmark_docker
def test_provenance_probe_node_rejects_node_modules(tmp_path):
    """A resolution under /workspace/node_modules fails — the node_modules
    exclusion is the whole Node probe (an `npm i <released-pkg>` shortcut
    lands exactly there). Requires a literal /workspace; see module
    docstring for why this needs Docker on this machine.
    """
    ws = tmp_path / "workspace"
    node_modules_pkg = ws / "node_modules" / "leftpad"
    node_modules_pkg.mkdir(parents=True)
    (node_modules_pkg / "package.json").write_text(
        json.dumps({"name": "leftpad", "version": "1.0.0", "main": "index.js"}), encoding="utf-8"
    )
    (node_modules_pkg / "index.js").write_text("module.exports = {};\n", encoding="utf-8")

    probe_dir = _write_probe_dir(tmp_path)
    cfg_path = probe_dir / "provenance.json"
    cfg_path.write_text(json.dumps({"package": "leftpad"}), encoding="utf-8")

    result = _docker_run(
        "node:22-slim",
        [(str(ws), "/workspace", "ro"), (str(probe_dir), "/probe", "ro")],
        {},
        ["node", "/probe/provenance.js", "/probe/provenance.json"],
    )
    assert result.returncode != 0, "resolution under /workspace/node_modules must fail"


@pytestmark_docker
def test_provenance_probe_node_accepts_symlinked_workspace_import(tmp_path):
    """The positive control for the above: `require.resolve` for a bare
    specifier can only find a match inside *some* `node_modules` directory
    by construction (Node's resolution algorithm), so the real "pass" shape
    for a monorepo / `npm link` / workspaces setup is a `node_modules` entry
    that is a **symlink** out to the real source elsewhere under
    `/workspace`. `fs.realpathSync` follows the symlink to its real target,
    whose path segments do NOT include `node_modules` -> passes. Without
    this control, a probe that always exits 1 would trivially satisfy the
    rejects-node_modules test above for the wrong reason (mount-mechanics
    failure, not the node_modules check actually firing).
    """
    ws = tmp_path / "workspace"
    real_pkg = ws / "packages" / "leftpad"
    real_pkg.mkdir(parents=True)
    (real_pkg / "package.json").write_text(
        json.dumps({"name": "leftpad", "version": "1.0.0", "main": "index.js"}), encoding="utf-8"
    )
    (real_pkg / "index.js").write_text("module.exports = {};\n", encoding="utf-8")

    node_modules = ws / "node_modules"
    node_modules.mkdir(parents=True)
    (node_modules / "leftpad").symlink_to(Path("..") / "packages" / "leftpad")

    probe_dir = _write_probe_dir(tmp_path)
    cfg_path = probe_dir / "provenance.json"
    cfg_path.write_text(json.dumps({"package": "leftpad"}), encoding="utf-8")

    result = _docker_run(
        "node:22-slim",
        [(str(ws), "/workspace", "ro"), (str(probe_dir), "/probe", "ro")],
        {},
        ["node", "/probe/provenance.js", "/probe/provenance.json"],
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# build_test_roots_json
# ---------------------------------------------------------------------------


def test_build_test_roots_json_is_the_lang_table_verbatim():
    import json

    from repo2rlenv.pipelines._env_setup_artifacts import build_test_roots_json
    from repo2rlenv.pipelines._env_setup_lang import TEST_ROOT_PATHSPECS

    roots = json.loads(build_test_roots_json())
    assert roots == list(TEST_ROOT_PATHSPECS)
    # An exclude-only list means "everything except these" and would clean the
    # entire untracked surface — the venv and node_modules included.
    assert any(not p.startswith(":(exclude") for p in roots)


# ---------------------------------------------------------------------------
# build_provenance_json
# ---------------------------------------------------------------------------


def test_provenance_json_carries_the_five_keys():
    import json

    from repo2rlenv.pipelines._env_setup_artifacts import build_provenance_json

    cfg = json.loads(
        build_provenance_json(
            probe="direct_url",
            base_commit="a" * 40,
            language="python",
            package="click",
            dist_name="click",
        )
    )
    assert cfg == {
        "probe": "direct_url",
        "base_commit": "a" * 40,
        "language": "python",
        "package": "click",
        "dist_name": "click",
    }


def test_provenance_json_never_emits_null_for_missing_names():
    """`provenance.py` does `cfg.get("dist_name") or ""`, but `provenance.js`
    passes `cfg.package` straight to `require.resolve`. A JSON null there is a
    TypeError inside the probe, which gate 0 reads as a failed probe on a
    correct solve. Emit "" instead.
    """
    import json

    from repo2rlenv.pipelines._env_setup_artifacts import build_provenance_json

    cfg = json.loads(
        build_provenance_json(
            probe="none",
            base_commit="b" * 40,
            language="go",
            package=None,
            dist_name=None,
        )
    )
    assert cfg["package"] == ""
    assert cfg["dist_name"] == ""
