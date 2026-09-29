"""The CLI loads `.env` from the working directory, wherever the package is installed."""

from __future__ import annotations

import os
import sys

import pytest

from repo2rlenv.cli import main

PROBE = "R2E_TEST_DOTENV_PROBE"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv(PROBE, raising=False)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    root = tmp_path / "project"
    (root / "nested").mkdir(parents=True)
    (root / ".env").write_text(f"{PROBE}=from-dotenv\n")
    yield root
    # load_dotenv writes os.environ directly, outside monkeypatch's bookkeeping.
    os.environ.pop(PROBE, None)


def _run_cli() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0


@pytest.mark.parametrize("cwd", [".", "nested"])
def test_loads_dotenv_from_working_directory(project, monkeypatch, cwd):
    # tmp_path is outside the package's install tree, so this fails if the
    # search starts from the installed module instead of the working directory.
    monkeypatch.chdir(project / cwd)
    _run_cli()
    assert os.environ[PROBE] == "from-dotenv"


def test_dotenv_does_not_override_existing_variables(project, monkeypatch):
    monkeypatch.setenv(PROBE, "from-shell")
    monkeypatch.chdir(project)
    _run_cli()
    assert os.environ[PROBE] == "from-shell"


def test_dotenv_loading_can_be_disabled(project, monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.chdir(project)
    _run_cli()
    assert PROBE not in os.environ


def test_undecodable_dotenv_warns_instead_of_failing(project, monkeypatch, capsys):
    (project / ".env").write_bytes(f"{PROBE}=caf\xe9\n".encode("latin-1"))
    monkeypatch.chdir(project)
    _run_cli()
    result = capsys.readouterr()
    assert result.out.startswith("repo2rlenv ")
    assert "could not load" in result.err
    assert PROBE not in os.environ


def test_unreadable_dotenv_warns_instead_of_failing(project, monkeypatch, capsys):
    dotenv = project / ".env"
    dotenv.chmod(0)
    if os.access(dotenv, os.R_OK):
        pytest.skip("permissions are not enforced for this user or platform")
    monkeypatch.chdir(project)
    try:
        _run_cli()
    finally:
        dotenv.chmod(0o600)
    assert "could not load" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot remove the working directory")
def test_deleted_working_directory_is_not_fatal(project, monkeypatch):
    monkeypatch.chdir(project / "nested")
    (project / "nested").rmdir()
    _run_cli()
