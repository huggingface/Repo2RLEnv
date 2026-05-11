"""Language detection from repo files."""

from __future__ import annotations

from pathlib import Path

from repo2rlenv.bootstrap.language import base_image_for, detect_language
from repo2rlenv.bootstrap.spec import LanguageHint


def test_detects_python(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("")
    assert detect_language(tmp_path) == LanguageHint.PYTHON


def test_detects_node(tmp_path: Path):
    (tmp_path / "package.json").write_text("{}")
    assert detect_language(tmp_path) == LanguageHint.NODE


def test_detects_go(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module x")
    assert detect_language(tmp_path) == LanguageHint.GO


def test_detects_rust(tmp_path: Path):
    (tmp_path / "Cargo.toml").write_text("[package]")
    assert detect_language(tmp_path) == LanguageHint.RUST


def test_priority_more_specific_wins(tmp_path: Path):
    """A repo with both Cargo.toml and Makefile should resolve to Rust, not C/C++."""
    (tmp_path / "Cargo.toml").write_text("")
    (tmp_path / "Makefile").write_text("")
    assert detect_language(tmp_path) == LanguageHint.RUST


def test_unknown_for_empty_dir(tmp_path: Path):
    assert detect_language(tmp_path) == LanguageHint.UNKNOWN


def test_base_image_per_language():
    assert base_image_for(LanguageHint.PYTHON).startswith("python:")
    assert base_image_for(LanguageHint.NODE).startswith("node:")
    assert base_image_for(LanguageHint.GO).startswith("golang:")
    assert base_image_for(LanguageHint.RUST).startswith("rust:")
    assert base_image_for(LanguageHint.UNKNOWN).startswith("ubuntu:")
