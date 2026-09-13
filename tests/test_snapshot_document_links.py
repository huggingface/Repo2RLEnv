from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from repo2rlenv.execution.python_repository import materialize_document_links
from repo2rlenv.spec.recipe_options import PythonRepositoryProfile


def profile(*links):
    return PythonRepositoryProfile(
        source_paths=["package"], test_paths=["tests"], materialize_document_links=list(links)
    )


def test_document_link_requires_explicit_selection_and_preserves_bytes(tmp_path):
    target = tmp_path / "docs/contributing.md"
    target.parent.mkdir()
    target.write_bytes(b"# Contributing\n\nRepository guidance.\n")
    link = tmp_path / "CONTRIBUTING.md"
    link.symlink_to("docs/contributing.md")
    assert materialize_document_links(tmp_path, profile()) == []
    assert link.is_symlink()
    records = materialize_document_links(tmp_path, profile("CONTRIBUTING.md"))
    assert not link.is_symlink()
    assert link.read_bytes() == target.read_bytes()
    assert records == [
        {
            "path": "CONTRIBUTING.md",
            "target": "docs/contributing.md",
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }
    ]
    assert materialize_document_links(tmp_path, profile("CONTRIBUTING.md")) == []


@pytest.mark.parametrize("kind", ["escape", "directory", "source", "missing", "cycle"])
def test_unsupported_link_is_rejected_before_any_materialization(tmp_path, kind):
    base = tmp_path / "snapshot"
    base.mkdir()
    good = base / "guide.md"
    good.write_text("guidance")
    safe = base / "README.md"
    safe.symlink_to("guide.md")
    link = base / "CONTRIBUTING.md"
    if kind == "escape":
        (tmp_path / "outside.md").write_text("outside")
        link.symlink_to("../outside.md")
    elif kind == "directory":
        (base / "folder.md").mkdir()
        link.symlink_to("folder.md")
    elif kind == "source":
        (base / "source.py").write_text("VALUE = 1\n")
        link.symlink_to("source.py")
    elif kind == "missing":
        link.symlink_to("missing.md")
    else:
        link.symlink_to("README-cycle.md")
        (base / "README-cycle.md").symlink_to("CONTRIBUTING.md")
    with pytest.raises(ValueError, match="Document link"):
        materialize_document_links(base, profile("README.md", "CONTRIBUTING.md"))
    assert safe.is_symlink()
    assert link.is_symlink()


@pytest.mark.parametrize("links", [["source.py"], ["README.md", "README.md"], ["../README.md"]])
def test_profile_rejects_non_document_duplicate_and_traversal_paths(links):
    with pytest.raises(ValidationError):
        profile(*links)
