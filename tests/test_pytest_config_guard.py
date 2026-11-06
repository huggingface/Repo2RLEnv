from __future__ import annotations

import subprocess

import pytest

from repo2rlenv.pipelines._pytest_config_guard import AgentChange, cargo_profile_env, protect


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
    with pytest.raises(AgentChange, match="Untrusted test settings"):
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


def test_package_json_runner_keys_are_restored_and_other_edits_kept(repository):
    import json

    root, commit, _ = repository
    base = {
        "name": "pkg",
        "version": "1.0.0",
        "main": "index.js",
        "scripts": {"test": "jest"},
        "jest": {"testEnvironment": "node"},
    }
    sha = commit({"package.json": json.dumps(base, indent=2) + "\n", "lib/package.json": "{}\n"})
    agent = {
        **base,
        "version": "1.0.1",
        "exports": {".": "./index.js"},
        "scripts": {"test": "exit 0"},
        "jest": {"setupFiles": ["./forge.js"]},
        "babel": {"plugins": ["./forge"]},
        "type": "module",
    }
    (root / "package.json").write_text(json.dumps(agent, indent=2) + "\n")
    # A nested package.json applies to its subtree: same rule.
    (root / "lib" / "package.json").write_text('{"type": "module", "main": "x.js"}\n')
    protect(root, sha)
    restored = json.loads((root / "package.json").read_text())
    assert restored["scripts"] == {"test": "jest"}
    assert restored["jest"] == {"testEnvironment": "node"}
    assert "babel" not in restored and "type" not in restored
    assert restored["version"] == "1.0.1" and restored["exports"] == {".": "./index.js"}
    assert json.loads((root / "lib" / "package.json").read_text()) == {"main": "x.js"}


def test_unparseable_or_unchanged_package_json(repository):
    root, commit, _ = repository
    text = '{\n    "name": "pkg",\n    "scripts": {"test": "jest"}\n}\n'
    sha = commit({"package.json": text})
    protect(root, sha)
    assert (root / "package.json").read_text() == text  # untouched, formatting kept
    (root / "package.json").write_text("{not json")
    protect(root, sha)
    assert (root / "package.json").read_text() == text


@pytest.mark.parametrize(
    "name", ["babel.config.js", ".babelrc", "vite.config.ts", ".swcrc", ".nycrc.json", ".c8rc"]
)
def test_more_runner_files_are_restored(repository, name):
    root, commit, _ = repository
    sha = commit({name: "base\n"})
    (root / name).write_text("agent\n")
    (root / "sub").mkdir(exist_ok=True)
    (root / "sub" / name).write_text("added\n")
    protect(root, sha)
    assert (root / name).read_text() == "base\n"
    assert not (root / "sub" / name).exists()


CARGO = """[package]
name = "calc"
version = "0.1.0"
edition = "2021"

[[test]]
name = "it"
path = "tests/it.rs"

[profile.test]
opt-level = 1
"""


@pytest.mark.parametrize(
    ("edit", "allowed"),
    [
        ('version = "0.1.0"', 'version = "0.2.0"'),  # legitimate
        ('[[test]]\nname = "it"', '[[test]]\nname = "it"\nharness = false'),
        ('edition = "2021"', 'edition = "2021"\nautotests = false'),
        ('edition = "2021"', 'edition = "2021"\nbuild = "build.rs"'),
        ("[profile.test]", '[patch.crates-io]\nitoa = { path = "x" }\n\n[profile.test]'),
    ],
)
def test_cargo_test_settings_must_match_base(repository, edit, allowed):
    root, commit, _ = repository
    sha = commit({"Cargo.toml": CARGO})
    (root / "Cargo.toml").write_text(CARGO.replace(edit, allowed))
    if "version" in edit:
        protect(root, sha)
    else:
        with pytest.raises(AgentChange):
            protect(root, sha)


def test_profiles_are_pinned_from_base(repository, tmp_path):
    root, commit, _ = repository
    sha = commit({"Cargo.toml": CARGO})
    # The agent's profile edits are irrelevant: the pins come from base.
    (root / "Cargo.toml").write_text(CARGO.replace("opt-level = 1", "debug-assertions = false"))
    env = tmp_path / "guard.env"
    protect(root, sha, str(env))
    assert env.read_text().splitlines() == [
        "CARGO_PROFILE_DEV_DEBUG_ASSERTIONS=true",
        "CARGO_PROFILE_DEV_OVERFLOW_CHECKS=true",
        "CARGO_PROFILE_DEV_OPT_LEVEL=0",
        "CARGO_PROFILE_TEST_DEBUG_ASSERTIONS=true",
        "CARGO_PROFILE_TEST_OVERFLOW_CHECKS=true",
        "CARGO_PROFILE_TEST_OPT_LEVEL=1",
    ]
    assert (
        cargo_profile_env(b'[profile.dev]\nopt-level = "s"\n')[2] == "CARGO_PROFILE_DEV_OPT_LEVEL=s"
    )


def test_godebug_lines_must_match_base_but_requires_may_change(repository):
    root, commit, _ = repository
    base = "module calc\n\ngo 1.22\n\nrequire example.com/a v1.0.0\n"
    sha = commit({"go.mod": base})
    (root / "go.mod").write_text(base.replace("v1.0.0", "v1.1.0"))
    protect(root, sha)
    (root / "go.mod").write_text(base + "\ngodebug (\n\tpanicnil=1\n)\n")
    with pytest.raises(AgentChange):
        protect(root, sha)


def test_unreadable_shared_configuration_is_the_agents(repository):
    root, commit, _ = repository
    sha = commit({"Cargo.toml": CARGO})
    (root / "Cargo.toml").write_text("[package\n")
    with pytest.raises(AgentChange):
        protect(root, sha)


def test_guard_reads_toml_without_tomllib(repository, monkeypatch, tmp_path):
    # Python < 3.11 under -I -S: no tomllib and no tomli. The built-in reader
    # must still tell a packaging edit from a test-settings edit.
    import sys

    monkeypatch.setitem(sys.modules, "tomllib", None)
    root, commit, _ = repository
    sha = commit(
        {
            "pyproject.toml": '[project]\nversion = "1.0"\n[tool.pytest.ini_options]\n'
            'addopts = "-q"\nmarkers = [\n  "slow: slow tests",\n]\n',
            "Cargo.toml": CARGO,
        }
    )
    pyproject = root / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace('"1.0"', '"1.1"'))
    protect(root, sha)
    pyproject.write_text(pyproject.read_text().replace('"-q"', '"-p fake"'))
    with pytest.raises(AgentChange):
        protect(root, sha)
    pyproject.write_text(pyproject.read_text().replace('"-p fake"', '"-q"'))
    (root / "Cargo.toml").write_text(CARGO.replace('name = "it"', 'name = "it"\nharness = false'))
    with pytest.raises(AgentChange):
        protect(root, sha)
    (root / "Cargo.toml").write_text(CARGO)
    env = tmp_path / "guard.env"
    protect(root, sha, str(env))
    assert "CARGO_PROFILE_TEST_OPT_LEVEL=1" in env.read_text().splitlines()


@pytest.mark.parametrize(
    ("base_files", "agent_files", "allowed"),
    [
        ({}, {"build.rs": "fn main() {}\n"}, False),  # cargo picks up a new build.rs
        ({"build.rs": "fn main() {}\n"}, {"build.rs": "fn main() { rewrite() }\n"}, False),
        ({"build.rs": "fn main() {}\n"}, {}, True),  # unchanged
        ({}, {"src/lib.rs": "pub fn f() {}\n"}, True),  # ordinary source edit
    ],
)
def test_cargo_build_script_must_match_base(repository, base_files, agent_files, allowed):
    root, commit, _ = repository
    sha = commit({"Cargo.toml": CARGO, **base_files})
    for name, text in agent_files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if allowed:
        protect(root, sha)
    else:
        with pytest.raises(AgentChange, match="build script"):
            protect(root, sha)


def test_custom_cargo_build_script_path_is_checked(repository):
    root, commit, _ = repository
    manifest = CARGO.replace('edition = "2021"', 'edition = "2021"\nbuild = "tools/gen.rs"')
    sha = commit({"Cargo.toml": manifest, "tools/gen.rs": "fn main() {}\n"})
    (root / "tools" / "gen.rs").write_text("fn main() { rewrite_tests() }\n")
    with pytest.raises(AgentChange, match="build script"):
        protect(root, sha)
