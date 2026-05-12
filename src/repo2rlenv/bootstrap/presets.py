"""Per-language presets to seed the bootstrap agent with known-good patterns.

The agent works for any repo, but giving it language-specific hints (where
the manifest file lives, what install command to try first, which sanity
check is fastest, what known gotchas to avoid) reliably cuts iterations and
cost by 30-50% on common stacks.

These are *hints*, not policy: the agent is free to ignore them when the
repo looks unusual. The prompts module injects them into the system prompt.

Keep this file dependency-free: imported during prompt construction in cold
paths, so we don't want it to drag in heavy modules.
"""

from __future__ import annotations

from dataclasses import dataclass

from repo2rlenv.bootstrap.spec import LanguageHint


@dataclass(slots=True, frozen=True)
class LanguagePreset:
    """Recipe of known-good patterns for a single language ecosystem."""

    base_image: str
    apt_packages: tuple[str, ...] = ()
    install_hints: tuple[str, ...] = ()
    sanity_checks: tuple[str, ...] = ()
    known_pitfalls: tuple[str, ...] = ()


# Common gotchas we've hit across pipelines — apply to every preset.
# Keep terse; the agent reads these inside its system prompt every turn.
_UNIVERSAL_PITFALLS: tuple[str, ...] = (
    "POSIX character classes like `[:(]` are malformed — write `[(:]` instead.",
    "`grep` on a non-existent dir exits 2 and looks like 'no match'; search from `.` only.",
    "Prefer `python -m pytest` over bare `pytest` — non-interactive shells often lack pytest on PATH.",
    "Bash backticks inside echo strings command-substitute; use single quotes for literal names.",
    "Local image digests aren't pullable via BuildKit; refer to images by tag when running locally.",
)


PRESETS: dict[LanguageHint, LanguagePreset] = {
    LanguageHint.PYTHON: LanguagePreset(
        base_image="python:3.12-slim",
        apt_packages=("git", "build-essential", "curl", "ca-certificates"),
        install_hints=(
            "Look for `pyproject.toml`, `setup.py`, `setup.cfg`, or `requirements.txt`.",
            "PREFER SYSTEM-WIDE installs over venvs — `pip install -e .[dev,test]` directly. "
            "The container IS the venv. System-wide installs survive `docker commit` AND "
            "the post-commit fresh-shell verify without any activation prefix.",
            "If you MUST use uv/venv (e.g. the project's deps force it), remember test_cmds "
            "will run in a fresh `bash -lc` shell — every test_cmd entry must include "
            "`. /workspace/.venv/bin/activate &&` as a prefix.",
            "Prefer `pip install -e .[dev,test]` or `[test]` extras when declared; "
            "fall back to `-e .` plus the explicit dev/test requirements file. "
            "Some projects declare optional components via separate extras (e.g. black's `[d]` "
            "extra installs blackd needed by tests) — check `pyproject.toml`'s `[project.optional-dependencies]`.",
            "After install, defensively run `pip install pytest pytest-xdist` — many repos "
            "declare these via extras the agent can miss.",
        ),
        sanity_checks=(
            "python -c 'import sys; print(sys.version)'",
            "python -m pytest --collect-only -q | head -50",
        ),
        known_pitfalls=(
            "Some packages need `xprocess`, `pytest-asyncio`, `pytest-trio`, etc. — "
            "if collection fails with import errors, install the missing plugin.",
            "Editable installs of namespaced packages require `pip install -e . --config-settings "
            "editable_mode=compat` on newer pip versions.",
            "Use `python -m pytest` (NEVER bare `pytest`) — agent's verifier shells often lack it on PATH.",
            "SHALLOW CLONE + version-from-git: the workspace is a depth=1 clone, so projects using "
            "`hatch-vcs` or `setuptools_scm` cannot derive a version from git tags. Symptom: "
            "`pip install` fails inside the build hook with a version detection error. "
            "Fix: prepend an env var BEFORE the install, e.g. "
            "`SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 pip install -e .` or "
            "`HATCH_VCS_PRETEND_VERSION=0.0.0 pip install -e .`. Do NOT try to sed pyproject.toml.",
            "CYTHON-COMPILED packages (aiohttp, lxml, sqlalchemy[c], pyzmq, ...): `setup.py` "
            "expects pre-generated `.c` files alongside the `.pyx` sources. A fresh `pip install` "
            "without those files fails with 'No such file or directory: <pkg>.c'. Fix: run the "
            "project's own codegen step BEFORE pip install. Most repos ship a `make cythonize` "
            "(check the Makefile); otherwise `pip install cython && cython <path>/*.pyx` works.",
        ),
    ),
    LanguageHint.NODE: LanguagePreset(
        base_image="node:22-slim",
        apt_packages=("git", "build-essential", "python3", "ca-certificates"),
        install_hints=(
            "Detect package manager from lockfile: `pnpm-lock.yaml` → pnpm, "
            "`yarn.lock` → yarn, `package-lock.json` or none → npm.",
            "Use `corepack enable` then `pnpm install` / `yarn install` / `npm ci`.",
            "Monorepos: look for `pnpm-workspace.yaml` or `workspaces` in `package.json`; "
            "install at the root, then `cd` into the target package.",
            "Native modules (better-sqlite3, sharp) need `python3` + `build-essential`.",
        ),
        sanity_checks=(
            "node --version && npm --version",
            "npm test -- --listTests 2>/dev/null | head -20 "
            "|| npx mocha --dry-run 2>/dev/null | head -20",
        ),
        known_pitfalls=(
            "Express uses `mocha` not `jest` — `npm test` runs mocha directly.",
            "`npm ci` requires a lockfile; if absent, fall back to `npm install`.",
            "`prepare` scripts run husky/lefthook hooks that may fail in CI; "
            "set `HUSKY=0` or `--ignore-scripts` if hooks block install.",
        ),
    ),
    LanguageHint.GO: LanguagePreset(
        base_image="golang:1.23",
        apt_packages=("git", "ca-certificates"),
        install_hints=(
            "Look for `go.mod`. Run `go mod download` to fetch deps, then `go build ./...`.",
            "Most Go repos pass `go test ./...` straight away — no separate install step.",
            "Pin GOFLAGS to avoid surprises: `export GOFLAGS=-mod=mod`.",
            "If the repo vendors deps (`vendor/` dir), use `go build -mod=vendor`.",
        ),
        sanity_checks=(
            "go version",
            "go test -list '.*' ./... 2>&1 | head -30",
        ),
        known_pitfalls=(
            "`go test ./...` is the convention but pkg-by-pkg may be needed for slow suites.",
            "Some CLIs build a binary first (`make build`); check `Makefile` for the test target.",
            "Cgo deps (e.g. SQLite via mattn/go-sqlite3) need `apt-get install build-essential`.",
        ),
    ),
    LanguageHint.RUST: LanguagePreset(
        base_image="rust:1-slim",
        apt_packages=("git", "build-essential", "pkg-config", "libssl-dev", "ca-certificates"),
        install_hints=(
            "Look for `Cargo.toml`. `cargo fetch` to download deps, then `cargo build`.",
            "`cargo test --no-run` compiles tests without executing — fastest signal that the env works.",
            "Workspaces declare members in the root `Cargo.toml`; `cargo build --workspace` covers all.",
            "On slim images, expect to apt-install `pkg-config` and `libssl-dev` for crates using OpenSSL.",
        ),
        sanity_checks=(
            "rustc --version && cargo --version",
            "cargo test --no-run 2>&1 | tail -20",
        ),
        known_pitfalls=(
            "First `cargo build` is slow (~5 min for big repos); the agent should not assume a stalled command.",
            "Some crates need a `nightly` toolchain; install via `rustup toolchain install nightly`.",
            "`cargo test --doc` is required to exercise doctest blocks but is often slow — skip unless needed.",
        ),
    ),
    LanguageHint.JAVA: LanguagePreset(
        base_image="eclipse-temurin:21-jdk",
        apt_packages=("git", "ca-certificates"),
        install_hints=(
            "Look for `pom.xml` (Maven) or `build.gradle[.kts]` (Gradle).",
            "Maven: `mvn -B -DskipTests=false test-compile` validates env without running tests.",
            "Gradle: `./gradlew testClasses` is the dry-run equivalent.",
        ),
        sanity_checks=(
            "java --version",
            "mvn --version 2>/dev/null || ./gradlew --version 2>/dev/null",
        ),
        known_pitfalls=(
            "Gradle wrapper may need execute bit: `chmod +x gradlew`.",
            "Some projects require a specific JDK (8/11/17/21); check `.java-version` or `pom.xml` `<maven.compiler.source>`.",
        ),
    ),
    LanguageHint.C_CPP: LanguagePreset(
        base_image="ubuntu:24.04",
        apt_packages=("git", "build-essential", "cmake", "pkg-config", "ca-certificates"),
        install_hints=(
            "Detect from `CMakeLists.txt`, `configure.ac`, `Makefile`, or `meson.build`.",
            "CMake: `cmake -S . -B build && cmake --build build -j`.",
            "Autotools: `./autogen.sh || autoreconf -i && ./configure && make`.",
        ),
        sanity_checks=("gcc --version && cmake --version",),
        known_pitfalls=(
            "Header-only deps via `apt-get install lib<x>-dev` — guess based on `pkg-config --list-all`.",
        ),
    ),
    LanguageHint.UNKNOWN: LanguagePreset(
        base_image="ubuntu:24.04",
        apt_packages=("git", "build-essential", "curl", "ca-certificates"),
        install_hints=(
            "No language marker detected. Start with `ls -la` and `cat README.md | head -80` to inspect.",
            "Look for `Makefile` targets first — they're usually the canonical entry point.",
        ),
        sanity_checks=(),
        known_pitfalls=(),
    ),
}


def preset_for(lang: LanguageHint) -> LanguagePreset:
    """Return the preset for a language, falling back to UNKNOWN."""
    return PRESETS.get(lang, PRESETS[LanguageHint.UNKNOWN])


def preset_hints_block(lang: LanguageHint) -> str:
    """Render the preset as a prompt-ready bullet list. Includes universal pitfalls."""
    p = preset_for(lang)
    parts: list[str] = []

    if p.apt_packages:
        parts.append("Useful apt packages (install if missing): " + ", ".join(p.apt_packages))

    if p.install_hints:
        parts.append("Install hints:")
        parts.extend(f"  - {h}" for h in p.install_hints)

    if p.sanity_checks:
        parts.append("Quick sanity checks once env is built:")
        parts.extend(f"  - `{c}`" for c in p.sanity_checks)

    pitfalls = (*p.known_pitfalls, *_UNIVERSAL_PITFALLS)
    if pitfalls:
        parts.append("Known pitfalls:")
        parts.extend(f"  - {pit}" for pit in pitfalls)

    return "\n".join(parts) if parts else "(no language-specific presets available)"


def universal_pitfalls() -> tuple[str, ...]:
    """Exposed for tests / docs that want the raw list."""
    return _UNIVERSAL_PITFALLS
