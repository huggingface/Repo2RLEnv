from __future__ import annotations

import unittest

import pytest

from repo2rlenv.pipelines.recipes.swe_smith.test_driver import run


@pytest.mark.parametrize("mode", ["replacement", "code"])
def test_framework_tampering_overrides_an_apparently_passing_suite(monkeypatch, mode):
    def passing_but_tampered(arguments):
        if mode == "replacement":
            monkeypatch.setattr(unittest.TestCase, "assertLess", lambda *args: None)
        else:
            monkeypatch.setattr(
                unittest.TestCase.assertLess, "__code__", (lambda *args: None).__code__
            )
        return 0

    monkeypatch.setattr(pytest, "main", passing_but_tampered)
    assert run(["trusted-fixture"]) == 254


@pytest.mark.parametrize("returncode", [0, 1, 2, 5])
def test_guard_preserves_unmodified_test_execution_outcome(monkeypatch, returncode):
    monkeypatch.setattr(pytest, "main", lambda arguments: returncode)
    assert run(["trusted-fixture"]) == returncode
