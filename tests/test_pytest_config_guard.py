from __future__ import annotations

import subprocess

import pytest

from repo2rlenv.pipelines._pytest_config_guard import protect


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()

    def git(*args):
        return (
            subprocess.check_output(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
                cwd=root,
            )
            .decode()
            .strip()
        )

    git("init", "-q")

    def commit(files):
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git("add", "-A")
        git("commit", "-qm", "base")
        return git("rev-parse", "HEAD")

    return root, commit, git


@pytest.mark.parametrize(
    ("name", "base", "legitimate", "malicious"),
    [
        (
            "pyproject.toml",
            '[project]\nversion = "1.0"\n[tool.pytest.ini_options]\naddopts = "-q"\n',
            '[project]\nversion = "1.1"\n[tool.pytest.ini_options]\naddopts = "-q"\n',
            '[project]\nversion = "1.1"\n[tool.pytest.ini_options]\naddopts = "-p fake"\n',
        ),
        (
            "pyproject.toml",
            'project = {version = "1.0"}\ntool.pytest.ini_options.addopts = "-q"\n',
            'project = {version = "1.1"}\ntool.pytest.ini_options.addopts = "-q"\n',
            'project = {version = "1.1"}\ntool.pytest.ini_options.addopts = "-p fake"\n',
        ),
        (
            "setup.cfg",
            "[metadata]\nversion = 1.0\n[tool:pytest]\naddopts = -q\n",
            "[metadata]\nversion = 1.1\n[tool:pytest]\naddopts = -q\n",
            "[metadata]\nversion = 1.1\n[tool:pytest]\naddopts = -p fake\n",
        ),
        (
            "tox.ini",
            "[tox]\nenvlist = py312\n[pytest]\naddopts = -q\n",
            "[tox]\nenvlist = py313\n[pytest]\naddopts = -q\n",
            "[tox]\nenvlist = py313\n[pytest]\naddopts = -p fake\n",
        ),
    ],
)
def test_shared_metadata_is_preserved_but_pytest_changes_are_rejected(
    repository, name, base, legitimate, malicious
):
    root, commit, _ = repository
    sha = commit({name: base})
    path = root / name
    path.write_text(legitimate)
    protect(root, sha)
    assert path.read_bytes() == legitimate.encode()
    path.write_text(malicious)
    with pytest.raises(ValueError, match="Untrusted pytest settings"):
        protect(root, sha)
    assert path.read_bytes() == malicious.encode()


def test_bootstrap_symlink_and_ordinary_file_symlink_survive(repository, tmp_path):
    root, commit, _ = repository
    sha = commit({"package.py": "VALUE = 1\n"})
    environment = tmp_path / "bootstrap-venv"
    environment.mkdir()
    (environment / "conftest.py").write_text("# not source\n")
    (root / ".venv").symlink_to(environment, target_is_directory=True)
    (root / "alias.py").symlink_to("package.py")
    protect(root, sha)
    assert (root / ".venv").is_symlink()
    assert (root / "alias.py").is_symlink()
    assert (environment / "conftest.py").read_text() == "# not source\n"


def test_trusted_hook_is_restored_and_cached_hooks_are_removed(repository):
    root, commit, _ = repository
    sha = commit({"tests/conftest.py": "# trusted baseline\n"})
    (root / "tests/conftest.py").write_text("# solver hook\n")
    cache = root / "tests/__pycache__"
    cache.mkdir()
    (cache / "conftest.cpython-312.pyc").write_bytes(b"untrusted bytecode")
    (root / "tests/conftest.pyc").write_bytes(b"sourceless hook")
    protect(root, sha)
    assert (root / "tests/conftest.py").read_text() == "# trusted baseline\n"
    assert not list(cache.iterdir())
    assert not (root / "tests/conftest.pyc").exists()


def test_tracked_internal_directory_symlink_is_preserved(repository):
    root, commit, git = repository
    commit({"tests/data/value.txt": "fixture\n"})
    (root / "fixture-data").symlink_to("tests/data", target_is_directory=True)
    git("add", "fixture-data")
    git("commit", "-qm", "fixture link")
    protect(root, git("rev-parse", "HEAD"))
    assert (root / "fixture-data").is_symlink()
    assert (root / "fixture-data/value.txt").read_text() == "fixture\n"


def test_restoring_hook_does_not_follow_file_symlink(repository, tmp_path):
    root, commit, _ = repository
    sha = commit({"tests/conftest.py": "# trusted\n"})
    outside = tmp_path / "outside.py"
    outside.write_text("# untouched\n")
    (root / "tests/conftest.py").unlink()
    (root / "tests/conftest.py").symlink_to(outside)
    protect(root, sha)
    assert not (root / "tests/conftest.py").is_symlink()
    assert (root / "tests/conftest.py").read_text() == "# trusted\n"
    assert outside.read_text() == "# untouched\n"


def test_workspace_git_configuration_cannot_redirect_base_read(repository, monkeypatch):
    root, commit, git = repository
    sha = commit({"conftest.py": "# trusted\n"})
    other = commit({"conftest.py": "# substituted\n"})
    git("replace", sha, other)
    git("config", "alias.cat-file", "!exit 99")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.pager")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "exit 99")
    protect(root, sha)
    assert (root / "conftest.py").read_text() == "# trusted\n"
