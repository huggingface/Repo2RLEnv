"""Per-language preset module: structure, content, and prompt injection."""

from __future__ import annotations

from repo2rlenv.bootstrap.presets import (
    PRESETS,
    LanguagePreset,
    preset_for,
    preset_hints_block,
    universal_pitfalls,
)
from repo2rlenv.bootstrap.prompts import system_prompt
from repo2rlenv.bootstrap.spec import LanguageHint


def test_every_language_has_preset():
    for lang in LanguageHint:
        p = preset_for(lang)
        assert isinstance(p, LanguagePreset)
        assert p.base_image, f"{lang} preset is missing a base image"


def test_presets_are_frozen_dataclasses():
    p = preset_for(LanguageHint.PYTHON)
    try:
        p.base_image = "other:latest"  # type: ignore[misc]
    except (AttributeError, Exception):
        return
    raise AssertionError("preset should be immutable")


def test_unknown_language_falls_back():
    p = preset_for(LanguageHint.UNKNOWN)
    assert p is PRESETS[LanguageHint.UNKNOWN]


def test_python_preset_pins_python_m_pytest_pattern():
    """The Python preset must teach the agent to avoid `pytest` on PATH."""
    p = preset_for(LanguageHint.PYTHON)
    joined = " ".join(p.install_hints + p.sanity_checks + p.known_pitfalls).lower()
    assert "python -m pytest" in joined, "Python preset must steer agent toward `python -m pytest`"


def test_universal_pitfalls_includes_posix_ere_gotcha():
    """The `[:(]` POSIX ERE bug should be in every prompt."""
    pitfalls = " ".join(universal_pitfalls())
    assert "[:(]" in pitfalls or "POSIX" in pitfalls


def test_preset_hints_block_includes_apt_packages_for_python():
    block = preset_hints_block(LanguageHint.PYTHON)
    assert "build-essential" in block
    assert "Install hints:" in block
    assert "Known pitfalls:" in block


def test_preset_hints_block_for_unknown_does_not_crash():
    block = preset_hints_block(LanguageHint.UNKNOWN)
    assert isinstance(block, str)
    assert len(block) > 0


def test_system_prompt_injects_preset_hints():
    """The renamed system_prompt() must include language-specific hints."""
    prompt = system_prompt(
        language=LanguageHint.PYTHON,
        base_image="python:3.12-slim",
        platform="linux/amd64",
    )
    # Python-specific hint must show up
    assert "pip install" in prompt
    # Universal pitfall must show up
    assert "python -m pytest" in prompt
