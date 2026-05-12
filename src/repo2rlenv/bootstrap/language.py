"""Auto-detect a repo's primary language from its files.

We don't try to be exhaustive — just enough to pick a sensible base image
and seed the agent prompt. Order matters: more-specific markers win.
"""

from __future__ import annotations

from pathlib import Path

from repo2rlenv.bootstrap.spec import LanguageHint

# Files listed in priority order. First match wins.
_MARKERS: list[tuple[LanguageHint, tuple[str, ...]]] = [
    (LanguageHint.RUST, ("Cargo.toml",)),
    (LanguageHint.GO, ("go.mod",)),
    (LanguageHint.NODE, ("package.json", "tsconfig.json", "pnpm-lock.yaml", "yarn.lock")),
    (LanguageHint.PYTHON, ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")),
    (LanguageHint.JAVA, ("pom.xml", "build.gradle", "build.gradle.kts")),
    (LanguageHint.C_CPP, ("CMakeLists.txt", "configure.ac", "Makefile", "meson.build")),
]


def detect_language(repo_root: Path) -> LanguageHint:
    """Return the inferred primary language. UNKNOWN if no markers match."""
    if not repo_root.exists():
        return LanguageHint.UNKNOWN
    names = {p.name for p in repo_root.iterdir() if p.is_file()}
    for lang, markers in _MARKERS:
        if any(m in names for m in markers):
            return lang
    return LanguageHint.UNKNOWN


def base_image_for(lang: LanguageHint) -> str:
    """Pick a sensible base image per language."""
    return {
        LanguageHint.PYTHON: "python:3.12-slim",
        LanguageHint.NODE: "node:22-slim",
        LanguageHint.GO: "golang:1.23",
        LanguageHint.RUST: "rust:1-slim",
        LanguageHint.JAVA: "eclipse-temurin:21-jdk",
        LanguageHint.C_CPP: "ubuntu:24.04",
        LanguageHint.UNKNOWN: "ubuntu:24.04",
    }[lang]


# Map GitHub's Linguist language names to our LanguageHint enum. Only the
# top-level "primary language" values we care about are listed — anything
# else maps to UNKNOWN. Case-insensitive lookup.
_GITHUB_LANGUAGE_MAP: dict[str, LanguageHint] = {
    "python": LanguageHint.PYTHON,
    "javascript": LanguageHint.NODE,
    "typescript": LanguageHint.NODE,
    "node": LanguageHint.NODE,
    "go": LanguageHint.GO,
    "rust": LanguageHint.RUST,
    "java": LanguageHint.JAVA,
    "kotlin": LanguageHint.JAVA,
    "scala": LanguageHint.JAVA,
    "c": LanguageHint.C_CPP,
    "c++": LanguageHint.C_CPP,
    "cpp": LanguageHint.C_CPP,
    "objective-c": LanguageHint.C_CPP,
    "objective-c++": LanguageHint.C_CPP,
}


def language_from_github_name(name: str | None) -> LanguageHint:
    """Translate a GitHub Linguist language name to a LanguageHint.

    Returns `LanguageHint.UNKNOWN` for null / unrecognized inputs so callers
    can decide whether to bail out or fall back to file-based detection.
    """
    if not name:
        return LanguageHint.UNKNOWN
    return _GITHUB_LANGUAGE_MAP.get(name.strip().lower(), LanguageHint.UNKNOWN)
