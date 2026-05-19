"""Tests for `registry.naming` — slug + tag construction."""

from __future__ import annotations

import re

import pytest

from repo2rlenv.registry.naming import (
    DEFAULT_BOOTSTRAP_PREFIX,
    build_image_ref,
    slugify_repo,
    split_ref,
)

_OCI_NAME = re.compile(r"^[a-z0-9]+((?:[._]|__|-+)[a-z0-9]+)*$")
_OCI_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


class TestSlugifyRepo:
    @pytest.mark.parametrize(
        "owner,name,expected",
        [
            ("pallets", "click", "r2e-bootstrap-pallets-click"),
            ("Pallets", "Click", "r2e-bootstrap-pallets-click"),
            ("huggingface", "trl", "r2e-bootstrap-huggingface-trl"),
            ("scikit-learn", "scikit-learn", "r2e-bootstrap-scikit-learn-scikit-learn"),
        ],
    )
    def test_basic(self, owner: str, name: str, expected: str) -> None:
        assert slugify_repo(owner, name) == expected

    def test_collapses_special_chars(self) -> None:
        out = slugify_repo("a.b_c", "d-e@f")
        assert _OCI_NAME.match(out)
        assert "@" not in out
        # The components are still recognisable
        assert "a-b-c" in out
        assert "d-e-f" in out

    def test_custom_prefix(self) -> None:
        out = slugify_repo("foo", "bar", prefix="custom")
        assert out == "custom-foo-bar"

    def test_truncates_long_input(self) -> None:
        long_name = "x" * 200
        out = slugify_repo("foo", long_name)
        assert len(out) <= 100
        assert _OCI_NAME.match(out)

    def test_empty_inputs_raise(self) -> None:
        with pytest.raises(ValueError):
            slugify_repo("", "bar")
        with pytest.raises(ValueError):
            slugify_repo("foo", "")


class TestBuildImageRef:
    def test_default_ghcr_shape(self) -> None:
        ref = build_image_ref(
            registry_prefix="ghcr.io/huggingface",
            owner="pallets",
            name="click",
            commit_sha="a1b2c3d4e5f67890",
        )
        assert ref == "ghcr.io/huggingface/r2e-bootstrap-pallets-click:a1b2c3d4e5f6"
        assert _OCI_TAG.match(ref.rpartition(":")[2])

    def test_with_options_hash(self) -> None:
        ref = build_image_ref(
            registry_prefix="ghcr.io/huggingface",
            owner="pallets",
            name="click",
            commit_sha="a1b2c3d4e5f6",
            options_hash="7d8e9f0123456789",
        )
        assert ref.endswith(":a1b2c3d4e5f6-7d8e9f01")

    def test_ecr_private_prefix(self) -> None:
        ref = build_image_ref(
            registry_prefix="123456789.dkr.ecr.us-east-1.amazonaws.com/r2e",
            owner="pallets",
            name="click",
            commit_sha="abcdef123456",
        )
        assert ref.startswith("123456789.dkr.ecr.us-east-1.amazonaws.com/r2e/")

    def test_localhost_registry(self) -> None:
        ref = build_image_ref(
            registry_prefix="localhost:5000",
            owner="foo",
            name="bar",
            commit_sha="0123456789ab",
        )
        assert ref.startswith("localhost:5000/")

    def test_truncates_sha_to_12(self) -> None:
        ref = build_image_ref(
            registry_prefix="ghcr.io/u",
            owner="o",
            name="n",
            commit_sha="A1B2C3D4E5F60123456789",
        )
        # truncated AND lowercased
        assert ref.endswith(":a1b2c3d4e5f6")

    def test_total_length_bound(self) -> None:
        ref = build_image_ref(
            registry_prefix="ghcr.io/very-long-org-name-that-is-still-realistic",
            owner="x" * 30,
            name="y" * 30,
            commit_sha="0123456789abcdef",
            options_hash="01234567",
        )
        assert len(ref) <= 255

    def test_empty_prefix_raises(self) -> None:
        with pytest.raises(ValueError):
            build_image_ref(
                registry_prefix="",
                owner="foo",
                name="bar",
                commit_sha="abc",
            )

    def test_empty_sha_raises(self) -> None:
        with pytest.raises(ValueError):
            build_image_ref(
                registry_prefix="ghcr.io/u",
                owner="foo",
                name="bar",
                commit_sha="",
            )

    def test_default_prefix_used(self) -> None:
        ref = build_image_ref(
            registry_prefix="ghcr.io/u",
            owner="foo",
            name="bar",
            commit_sha="abc123def456",
        )
        assert DEFAULT_BOOTSTRAP_PREFIX in ref


class TestSplitRef:
    def test_round_trip(self) -> None:
        ref = build_image_ref(
            registry_prefix="ghcr.io/huggingface",
            owner="pallets",
            name="click",
            commit_sha="a1b2c3d4e5f6",
        )
        prefix, image, tag = split_ref(ref)
        assert prefix == "ghcr.io/huggingface"
        assert image == "r2e-bootstrap-pallets-click"
        assert tag == "a1b2c3d4e5f6"

    def test_missing_tag_raises(self) -> None:
        with pytest.raises(ValueError):
            split_ref("ghcr.io/foo/bar")

    def test_missing_host_raises(self) -> None:
        with pytest.raises(ValueError):
            split_ref("foo:tag")
