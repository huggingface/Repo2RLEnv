"""Unit tests for `pipelines/_env_setup_lang.py` — the per-language table.

Covers: `bare_base_image` delegation (no local base-image literals), probe-kind
coverage for every `LanguageHint`, package/dist-name resolution asymmetry, and
the fixed (non-tree-filtered) test-root pathspec list.
"""

from __future__ import annotations

import inspect
import json

import pytest

from repo2rlenv.bootstrap.language import base_image_for
from repo2rlenv.bootstrap.spec import LanguageHint
from repo2rlenv.pipelines import _env_setup_lang
from repo2rlenv.pipelines._env_setup_lang import (
    PROBE_DIRECT_URL,
    PROBE_NONE,
    PROBE_PATH,
    TEST_ROOT_PATHSPECS,
    bare_base_image,
    probe_kind_for,
    resolve_package_names,
)
from repo2rlenv.spec.input import BootstrapSpec

# ---------------------------------------------------------------------------
# No second base-image table
# ---------------------------------------------------------------------------

_FORBIDDEN_IMAGE_LITERALS = (
    "python:",
    "node:",
    "golang:",
    "rust:",
    "eclipse-temurin:",
    "ubuntu:",
)


def test_lang_module_has_no_base_image_literals():
    source = inspect.getsource(_env_setup_lang)
    for literal in _FORBIDDEN_IMAGE_LITERALS:
        assert literal not in source, f"found forbidden base-image literal {literal!r}"


def test_bare_base_image_honors_spec_override():
    spec = BootstrapSpec(base_image="alpine:3.20")
    assert bare_base_image(spec, LanguageHint.PYTHON) == "alpine:3.20"


@pytest.mark.parametrize("lang", list(LanguageHint))
def test_bare_base_image_falls_back_to_base_image_for(lang):
    spec = BootstrapSpec()
    assert spec.base_image is None
    assert bare_base_image(spec, lang) == base_image_for(lang)


# ---------------------------------------------------------------------------
# Probe kind coverage
# ---------------------------------------------------------------------------

_EXPECTED_PROBE_KIND = {
    LanguageHint.PYTHON: PROBE_DIRECT_URL,
    LanguageHint.NODE: PROBE_PATH,
    LanguageHint.GO: PROBE_NONE,
    LanguageHint.RUST: PROBE_NONE,
    LanguageHint.JAVA: PROBE_NONE,
    LanguageHint.C_CPP: PROBE_NONE,
    LanguageHint.UNKNOWN: PROBE_NONE,
}


def test_probe_kind_covers_every_language():
    # every LanguageHint member is covered by the expectation table above
    assert set(_EXPECTED_PROBE_KIND) == set(LanguageHint)
    for lang, expected in _EXPECTED_PROBE_KIND.items():
        assert probe_kind_for(lang) == expected


def test_probe_kind_is_never_na():
    for lang in LanguageHint:
        kind = probe_kind_for(lang)
        assert kind in {PROBE_DIRECT_URL, PROBE_PATH, PROBE_NONE}
        assert kind != "n/a"


# ---------------------------------------------------------------------------
# Test-root pathspec list
# ---------------------------------------------------------------------------


def test_test_roots_include_unmatched_config_surface(tmp_path):
    # A repo with no conftest.py at base_commit at all.
    repo_root = tmp_path / "bare_repo"
    repo_root.mkdir()
    assert not (repo_root / "conftest.py").exists()

    # The list is fixed at emit time — it is NOT derived by scanning repo_root,
    # so a repo lacking conftest.py still gets both pathspecs that would clean
    # one if an agent planted it later.
    assert "conftest.py" in TEST_ROOT_PATHSPECS
    assert ":(glob)**/conftest.py" in TEST_ROOT_PATHSPECS
    assert len(TEST_ROOT_PATHSPECS) > 0


def test_test_roots_exclude_install_dirs():
    excludes = {
        ":(exclude,glob).venv/**",
        ":(exclude,glob)node_modules/**",
        ":(exclude,glob)**/site-packages/**",
    }
    assert excludes <= set(TEST_ROOT_PATHSPECS)

    positives = [p for p in TEST_ROOT_PATHSPECS if not p.startswith(":(exclude")]
    assert len(positives) > 0, "an exclude-only list would clean the entire untracked surface"


def test_test_roots_json_roundtrip():
    # Sanity: the list is plain-JSON-serializable, as tests/test_roots.json needs.
    dumped = json.dumps(list(TEST_ROOT_PATHSPECS))
    assert json.loads(dumped) == list(TEST_ROOT_PATHSPECS)


# ---------------------------------------------------------------------------
# package / dist_name resolution asymmetry
# ---------------------------------------------------------------------------


def test_resolve_package_names_python_pyproject_full_match(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "pyproject.toml").write_text('[project]\nname = "Django"\n', encoding="utf-8")
    (repo_root / "django").mkdir()
    (repo_root / "django" / "__init__.py").write_text("", encoding="utf-8")

    package, dist_name = resolve_package_names(repo_root, LanguageHint.PYTHON)
    assert package == "django"
    assert dist_name == "Django"


def test_resolve_package_names_missing_dist_name_still_resolves_package_pyproject(tmp_path):
    """Asymmetry (RFC 0008 sec7c, lines 1214-1217): a missing dist_name must
    not degrade package resolution. No [project].name in pyproject.toml at
    all (dist_name is None), but [tool.setuptools].packages explicitly
    declares the package — resolved from config, not from a directory scan."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "pyproject.toml").write_text(
        '[tool.setuptools]\npackages = ["widget"]\n', encoding="utf-8"
    )

    package, dist_name = resolve_package_names(repo_root, LanguageHint.PYTHON)
    assert package == "widget"
    assert dist_name is None


def test_resolve_package_names_missing_dist_name_still_resolves_package_setup_cfg(tmp_path):
    """Same asymmetry via setup.cfg: [options].packages declared, no
    [metadata].name at all."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "setup.cfg").write_text("[options]\npackages = widget\n", encoding="utf-8")

    package, dist_name = resolve_package_names(repo_root, LanguageHint.PYTHON)
    assert package == "widget"
    assert dist_name is None


def test_resolve_package_names_missing_package_ships_none(tmp_path):
    """dist_name is declared but no matching import folder or build-config
    declaration confirms a package name — package is None (unresolved), so
    the caller ships probe='none' rather than any guess."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "pyproject.toml").write_text('[project]\nname = "Django"\n', encoding="utf-8")
    # No django/ or src/django/ folder, and no [tool.setuptools] declaration.

    package, dist_name = resolve_package_names(repo_root, LanguageHint.PYTHON)
    assert package is None
    assert dist_name == "Django"


def test_resolve_package_names_does_not_guess_from_directory_layout(tmp_path):
    """Regression for RFC 0008 sec6a: a single plausible-looking top-level
    directory containing __init__.py, with NO config declaring it anywhere
    (no pyproject.toml, setup.cfg, or package.json at all), must still ship
    unresolved rather than being silently accepted as `package`."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "widget").mkdir()
    (repo_root / "widget" / "__init__.py").write_text("", encoding="utf-8")

    package, dist_name = resolve_package_names(repo_root, LanguageHint.PYTHON)
    assert package is None
    assert dist_name is None


def test_resolve_package_names_find_directive_not_trusted(tmp_path):
    """setup.cfg's `packages = find:` (and `find_namespace:`) is setuptools'
    own auto-discovery directive, not a declared name, and must not be
    treated as one."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "setup.cfg").write_text("[options]\npackages = find:\n", encoding="utf-8")

    package, dist_name = resolve_package_names(repo_root, LanguageHint.PYTHON)
    assert package is None
    assert dist_name is None


def test_resolve_package_names_python_setup_cfg(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "setup.cfg").write_text("[metadata]\nname = Flask-Extra\n", encoding="utf-8")
    (repo_root / "src").mkdir()
    (repo_root / "src" / "flask_extra").mkdir()
    (repo_root / "src" / "flask_extra" / "__init__.py").write_text("", encoding="utf-8")

    package, dist_name = resolve_package_names(repo_root, LanguageHint.PYTHON)
    assert package == "flask_extra"
    assert dist_name == "Flask-Extra"


def test_resolve_package_names_node_package_json(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "package.json").write_text(json.dumps({"name": "left-pad"}), encoding="utf-8")

    package, dist_name = resolve_package_names(repo_root, LanguageHint.NODE)
    assert package == "left-pad"
    assert dist_name == "left-pad"


def test_resolve_package_names_node_missing_package_json(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    package, dist_name = resolve_package_names(repo_root, LanguageHint.NODE)
    assert package is None
    assert dist_name is None


@pytest.mark.parametrize(
    "lang",
    [
        LanguageHint.GO,
        LanguageHint.RUST,
        LanguageHint.JAVA,
        LanguageHint.C_CPP,
        LanguageHint.UNKNOWN,
    ],
)
def test_resolve_package_names_unsupported_languages_are_none(tmp_path, lang):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    package, dist_name = resolve_package_names(repo_root, lang)
    assert package is None
    assert dist_name is None
