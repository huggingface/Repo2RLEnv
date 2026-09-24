"""Attribution must be discoverable and declared in distribution metadata."""

from __future__ import annotations

import tomllib
from pathlib import Path

from repo2rlenv.pipelines.recipes.catalog import IMPLEMENTATIONS, recipes

ROOT = Path(__file__).resolve().parents[1]


def test_every_retained_recipe_license_is_declared():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["license"] == "Apache-2.0 AND MIT"
    declared = {path for pattern in project["license-files"] for path in ROOT.glob(pattern)}
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
    assert ROOT / "THIRD_PARTY_NOTICES.md" in declared
    for recipe in recipes():
        if recipe.id not in IMPLEMENTATIONS:
            continue
        folder = ROOT / "src/repo2rlenv/pipelines/recipes" / recipe.id
        assert (folder / "provenance.md").is_file()
        reference = recipe.upstream.get("commit") or recipe.upstream["version"]
        assert reference in notices
        license_file = folder / "UPSTREAM_LICENSE"
        if recipe.upstream.get("retained_material", True):
            assert license_file in declared
            assert recipe.upstream["license"] in {"MIT", "Apache-2.0"}
        else:
            assert not license_file.exists()
            assert recipe.upstream["license"] == "NOASSERTION"
    assert ROOT / "src/repo2rlenv/pipelines/recipes/scaler/UPSTREAM_NOTICE" in declared
