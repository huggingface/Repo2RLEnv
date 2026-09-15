import tomllib

import pytest

from repo2rlenv.pipelines.recipes.repository.version import freeze_version


def project(tmp_path):
    (tmp_path / "pyproject.toml").write_text("""[project]
name = "some-library"
dynamic = ["version", "readme"]
[tool.setuptools-git-versioning]
enabled = true
[tool.setuptools]
packages = ["lib", "lib.tests", "lib.tests.nested", "lib.tests_api"]
""")
    info = tmp_path / "some_library.egg-info" / "PKG-INFO"
    info.parent.mkdir()
    info.write_text("Name: some-library\nVersion: 2.1.0+3.gabcdef.dirty\n\nDescription\n")
    return info


def test_freeze_uses_observed_version_and_omits_private_packages(tmp_path):
    info = project(tmp_path)
    original = (tmp_path / "pyproject.toml").read_bytes()
    files, evidence = freeze_version(tmp_path, test_paths=["lib/tests"])
    config = tomllib.loads(files["pyproject.toml"].decode())
    assert config["project"]["version"] == "2.1.0"
    assert config["project"]["dynamic"] == ["readme"]
    assert config["tool"]["setuptools-git-versioning"]["enabled"] is False
    assert config["tool"]["setuptools"]["packages"] == ["lib", "lib.tests_api"]
    assert b"gabcdef" not in files[info.relative_to(tmp_path).as_posix()]
    assert evidence["basis"] == "recorded PKG-INFO"
    assert (tmp_path / "pyproject.toml").read_bytes() == original


def test_freeze_rejects_missing_or_conflicting_version_evidence(tmp_path):
    info = project(tmp_path)
    info.unlink()
    with pytest.raises(ValueError, match="unambiguous"):
        freeze_version(tmp_path, test_paths=[])
    info.write_text("Name: some-library\nVersion: 1.0\n")
    other = tmp_path / "src/some_library.egg-info/PKG-INFO"
    other.parent.mkdir(parents=True)
    other.write_text("Name: some_library\nVersion: 2.0\n")
    with pytest.raises(ValueError, match="unambiguous"):
        freeze_version(tmp_path, test_paths=[])
