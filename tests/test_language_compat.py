"""Pipeline ↔ repo-language compatibility: helper + GitHub-name mapper.

Pure-logic tests; no network, no Docker, no LLM.
"""

from __future__ import annotations

import logging

import pytest

from repo2rlenv.bootstrap.language import language_from_github_name
from repo2rlenv.bootstrap.spec import LanguageHint
from repo2rlenv.pipelines.base import (
    LanguageMismatchError,
    check_language_compatibility,
)

# ----------------------------------------------------------------------------
# check_language_compatibility — the core dispatcher helper
# ----------------------------------------------------------------------------


class _LangAgnostic:
    name = "pr_runtime"
    supported_languages = None


class _PythonOnly:
    name = "equivalence_tests"
    supported_languages = frozenset({LanguageHint.PYTHON})


class _DualLang:
    """Hypothetical pipeline supporting both Python and Node — exercise multi-lang."""

    name = "hypothetical_dual"
    supported_languages = frozenset({LanguageHint.PYTHON, LanguageHint.NODE})


def test_agnostic_pipeline_accepts_any_language():
    # No exception for any LanguageHint when supported_languages is None
    for lang in LanguageHint:
        check_language_compatibility(_LangAgnostic, lang)  # must not raise


def test_python_only_accepts_python():
    check_language_compatibility(_PythonOnly, LanguageHint.PYTHON)  # must not raise


def test_python_only_rejects_go_without_force():
    with pytest.raises(LanguageMismatchError) as exc:
        check_language_compatibility(_PythonOnly, LanguageHint.GO)
    msg = str(exc.value)
    assert "equivalence_tests" in msg
    assert "python" in msg.lower()
    assert "go" in msg.lower()
    assert "--force-language" in msg


def test_python_only_rejects_rust_without_force():
    with pytest.raises(LanguageMismatchError):
        check_language_compatibility(_PythonOnly, LanguageHint.RUST)


def test_python_only_rejects_node_without_force():
    with pytest.raises(LanguageMismatchError):
        check_language_compatibility(_PythonOnly, LanguageHint.NODE)


def test_python_only_with_force_warns_then_returns(caplog):
    caplog.set_level(logging.WARNING)
    # Force overrides the strict check
    check_language_compatibility(_PythonOnly, LanguageHint.GO, force=True)
    assert any("requires" in rec.message.lower() for rec in caplog.records), (
        "force=True must emit a warning so the user knows the check was bypassed"
    )


def test_dual_lang_accepts_either():
    check_language_compatibility(_DualLang, LanguageHint.PYTHON)
    check_language_compatibility(_DualLang, LanguageHint.NODE)
    with pytest.raises(LanguageMismatchError):
        check_language_compatibility(_DualLang, LanguageHint.RUST)


def test_unknown_language_rejected_by_restricted_pipeline():
    """If GitHub returns no primary language, we shouldn't silently let a
    Python-only pipeline through. UNKNOWN is treated as "doesn't match"."""
    with pytest.raises(LanguageMismatchError):
        check_language_compatibility(_PythonOnly, LanguageHint.UNKNOWN)


def test_unknown_language_with_force_allowed():
    """Force overrides everything — including unknown."""
    check_language_compatibility(_PythonOnly, LanguageHint.UNKNOWN, force=True)


# ----------------------------------------------------------------------------
# language_from_github_name — GitHub Linguist name → LanguageHint
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "github_name,expected",
    [
        ("Python", LanguageHint.PYTHON),
        ("python", LanguageHint.PYTHON),
        ("PYTHON", LanguageHint.PYTHON),
        ("JavaScript", LanguageHint.NODE),
        ("TypeScript", LanguageHint.NODE),
        ("Go", LanguageHint.GO),
        ("Rust", LanguageHint.RUST),
        ("Java", LanguageHint.JAVA),
        ("Kotlin", LanguageHint.JAVA),
        ("Scala", LanguageHint.JAVA),
        ("C", LanguageHint.C_CPP),
        ("C++", LanguageHint.C_CPP),
        ("Objective-C", LanguageHint.C_CPP),
        ("Haskell", LanguageHint.UNKNOWN),  # not in the map
        ("", LanguageHint.UNKNOWN),
        (None, LanguageHint.UNKNOWN),
    ],
)
def test_github_name_to_language_hint(github_name, expected):
    assert language_from_github_name(github_name) == expected


def test_github_name_handles_whitespace():
    assert language_from_github_name("  Python  ") == LanguageHint.PYTHON
