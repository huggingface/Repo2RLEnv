"""Per-language table for the `env_setup` pipeline.

Three responsibilities, all data-heavy:

  1. `bare_base_image` — a one-line delegation to `bootstrap.language.base_image_for`
     (see the module docstring note below on why there is no base-image table here).
  2. `probe_kind_for` — which provenance-probe mechanism (§7c of RFC 0008) applies
     per language: `direct_url`, `path`, or `none`. There is no `"n/a"`.
  3. `resolve_package_names` — resolve the *import* name (`package`) and the
     *distribution* name (`dist_name`) at emit time from `pyproject.toml` /
     `setup.cfg` / `package.json`. Never recovered at run time —
     `importlib.metadata.packages_distributions()` misses `.pth`-style editable
     installs.

Plus `TEST_ROOT_PATHSPECS`, the fixed emit-time pathspec list for
`tests/test_roots.json` (RFC 0008 §7f). It is intentionally **not** tree-filtered:
`git clean` accepts pathspecs that match nothing and exits 0, so listing
`conftest.py` for a repo that doesn't have one yet costs nothing and still
catches one an agent plants later. Tree-filtering it (keep-only-if-already-exists)
would defeat that exact case.

This module must never grow a second base-image table. RFC 0008 §1 exists
specifically because `bootstrap/language.py`'s `base_image_for` is the single
source of truth for base images; a second literal table here (even one that
"agrees" with it today) would be the same class of defect that motivated that
section. `bare_base_image` takes the `BootstrapSpec`, not just the language,
because that is the same expression `bootstrap/runner.py` evaluates
(`spec.base_image or base_image_for(lang)`) — reading only the language would
disagree with the reconstructed Dockerfile on every ref where `--base-image`
overrides the default.

----------------------------------------------------------------------------
Acknowledgment
----------------------------------------------------------------------------
The per-language provenance-probe posture and test-root discovery model this
module encodes are informed by:

  Repo2Run (ByteDance, arXiv:2502.13681)
  https://github.com/bytedance/Repo2Run    (Apache-2.0)

  SetupBench (Microsoft, arXiv:2507.09063)
  https://github.com/microsoft/SetupBench    (MIT)

  EnvBench (JetBrains Research, ICLR '25 DL4Code, arXiv:2503.14443)
  https://github.com/JetBrains-Research/EnvBench    (MIT)

This module is an INDEPENDENT IMPLEMENTATION — no code is copied from any of
the three. It reuses only the general shape (per-language setup posture,
scored by whether the environment is genuinely reproducible) and reimplements
it from scratch against Python stdlib (`tomllib`, `configparser`, `json`).
None of the upstream licenses apply to this file; Repo2RLEnv is Apache-2.0.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import configparser
import json
import tomllib
from pathlib import Path

from repo2rlenv.bootstrap.language import base_image_for
from repo2rlenv.bootstrap.spec import LanguageHint
from repo2rlenv.spec.input import BootstrapSpec

# ---------------------------------------------------------------------------
# 1. Bare base image — delegated, no local literals
# ---------------------------------------------------------------------------


def bare_base_image(spec: BootstrapSpec, lang: LanguageHint) -> str:
    """The base image `bootstrap/runner.py` resolved for this bootstrap.

    Same expression the runner evaluates. Takes `spec` (not just `lang`) so a
    `--base-image` override is honored identically here and there.
    """
    return spec.base_image or base_image_for(lang)


# ---------------------------------------------------------------------------
# 2. Provenance-probe kind per language
# ---------------------------------------------------------------------------

PROBE_DIRECT_URL = "direct_url"
PROBE_PATH = "path"
PROBE_NONE = "none"

_PROBE_KIND: dict[LanguageHint, str] = {
    LanguageHint.PYTHON: PROBE_DIRECT_URL,
    LanguageHint.NODE: PROBE_PATH,
    LanguageHint.GO: PROBE_NONE,
    LanguageHint.RUST: PROBE_NONE,
    LanguageHint.JAVA: PROBE_NONE,
    LanguageHint.C_CPP: PROBE_NONE,
    LanguageHint.UNKNOWN: PROBE_NONE,
}


def probe_kind_for(lang: LanguageHint) -> str:
    """Which provenance-probe mechanism applies to this language.

    Go and Rust are `none` on purpose: `go test ./...` / `cargo test` compile
    from the `/workspace` source tree by construction, so the
    substitute-the-released-package shortcut this probe blocks doesn't exist
    there. Java and C/C++ are `none` because we have no probe we'd trust.
    """
    return _PROBE_KIND.get(lang, PROBE_NONE)


# ---------------------------------------------------------------------------
# 3. package / dist_name resolution — baked at emit time
# ---------------------------------------------------------------------------

# Directories that are never themselves the target package, scanned when no
# config file gives us a name to confirm against.
_NON_PACKAGE_DIRS = {
    "tests",
    "test",
    "docs",
    "doc",
    "scripts",
    "examples",
    "example",
    "build",
    "dist",
    "venv",
    ".venv",
    "node_modules",
    ".git",
    "__pycache__",
}


def resolve_package_names(repo_root: Path, lang: LanguageHint) -> tuple[str | None, str | None]:
    """Resolve `(package, dist_name)` at emit time. Never re-derived at run time.

    `package` is the *import* name (e.g. `django`); `dist_name` is the
    *distribution* name (e.g. `Django`). Only `package` is load-bearing: a
    missing `dist_name` costs nothing on the `direct_url` probe (it falls
    through to the import check), but a missing `package` leaves the probe
    nothing to check on either rung.

    Only Python and Node have a resolver; every other language returns
    `(None, None)` (their `probe_kind_for` is always `none`, so there is
    nothing to resolve for).
    """
    if lang == LanguageHint.PYTHON:
        return _resolve_python_names(repo_root)
    if lang == LanguageHint.NODE:
        return _resolve_node_names(repo_root)
    return None, None


def _python_dist_name(repo_root: Path) -> str | None:
    pyproject = repo_root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError):
            data = {}
        name = data.get("project", {}).get("name") or data.get("tool", {}).get("poetry", {}).get(
            "name"
        )
        if name:
            return str(name)

    setup_cfg = repo_root / "setup.cfg"
    if setup_cfg.is_file():
        parser = configparser.ConfigParser()
        try:
            parser.read(setup_cfg, encoding="utf-8")
            name = parser.get("metadata", "name", fallback=None)
        except configparser.Error:
            name = None
        if name:
            return name

    return None


def _confirm_python_package(repo_root: Path, candidate: str) -> str | None:
    for base in (repo_root / "src", repo_root):
        if (base / candidate / "__init__.py").is_file():
            return candidate
    return None


def _scan_single_python_package(repo_root: Path) -> str | None:
    """Best-effort fallback: exactly one plausible top-level package."""
    for base in (repo_root / "src", repo_root):
        if not base.is_dir():
            continue
        candidates = [
            p.name
            for p in base.iterdir()
            if p.is_dir()
            and (p / "__init__.py").is_file()
            and p.name not in _NON_PACKAGE_DIRS
            and not p.name.startswith(".")
        ]
        if len(candidates) == 1:
            return candidates[0]
    return None


def _resolve_python_names(repo_root: Path) -> tuple[str | None, str | None]:
    dist_name = _python_dist_name(repo_root)
    package: str | None = None
    if dist_name:
        candidate = dist_name.lower().replace("-", "_")
        package = _confirm_python_package(repo_root, candidate)
    if package is None:
        package = _scan_single_python_package(repo_root)
    return package, dist_name


def _resolve_node_names(repo_root: Path) -> tuple[str | None, str | None]:
    package_json = repo_root / "package.json"
    if not package_json.is_file():
        return None, None
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, None
    name = data.get("name")
    if not name:
        return None, None
    # npm has no dist/import split — the package.json `name` is both.
    return name, name


# ---------------------------------------------------------------------------
# 4. Test-root discovery — fixed emit-time pathspec list, never tree-filtered
# ---------------------------------------------------------------------------

TEST_ROOT_PATHSPECS: tuple[str, ...] = (
    # conventional test roots
    "tests/",
    "test/",
    "spec/",
    "t/",
    "__tests__/",
    # root config surface
    "conftest.py",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "jest.config.*",
    # recursive hook surface — a hook planted in src/ or a subpackage is
    # cleaned too, not only one at the root
    ":(glob)**/conftest.py",
    # exclusions for the agent's own install directories. `git clean` here
    # runs without `-x`, so an *ignored* .venv is already safe — but not every
    # repo ignores it, and for those the recursive glob above reaches straight
    # into it and deletes conftest.py files shipped by installed packages,
    # i.e. part of the agent's solve.
    ":(exclude,glob).venv/**",
    ":(exclude,glob)node_modules/**",
    ":(exclude,glob)**/site-packages/**",
)
