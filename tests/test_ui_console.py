"""ui.console: CLI output must survive a stdout that can't encode the UI glyphs."""

from __future__ import annotations

import io
import sys

import pytest

from repo2rlenv.ui.console import console, ensure_utf8_output


def _stream(encoding: str) -> tuple[io.BytesIO, io.TextIOWrapper]:
    raw = io.BytesIO()
    # newline="\n": no \n -> \r\n translation, so bytes are identical on every OS
    return raw, io.TextIOWrapper(raw, encoding=encoding, newline="\n")


@pytest.fixture(autouse=True)
def _no_pythonioencoding(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)


@pytest.mark.parametrize("name", ["stdout", "stderr"])
def test_non_utf8_stream_is_switched_to_utf8(monkeypatch: pytest.MonkeyPatch, name: str):
    raw, stream = _stream("cp1252")
    monkeypatch.setattr(sys, name, stream)

    ensure_utf8_output()
    stream.write("✓ ✗ ⚠ ─\n")
    stream.flush()

    assert raw.getvalue().decode("utf-8") == "✓ ✗ ⚠ ─\n"


def test_console_glyphs_print_to_a_cp1252_stdout(monkeypatch: pytest.MonkeyPatch):
    """Windows pipes/redirects are cp1252: console.success used to raise
    UnicodeEncodeError on its ✓ and the command exited 1."""
    raw, stream = _stream("cp1252")
    monkeypatch.setattr(sys, "stdout", stream)

    ensure_utf8_output()
    console.success("all 1 tasks valid")
    stream.flush()

    assert "✓ all 1 tasks valid" in raw.getvalue().decode("utf-8")


def test_explicit_pythonioencoding_is_respected(monkeypatch: pytest.MonkeyPatch):
    _, stream = _stream("cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")

    ensure_utf8_output()

    assert stream.encoding == "cp1252"


def test_utf8_and_non_reconfigurable_streams_are_left_alone(monkeypatch: pytest.MonkeyPatch):
    _, utf8 = _stream("UTF8")
    monkeypatch.setattr(sys, "stdout", utf8)
    monkeypatch.setattr(sys, "stderr", io.StringIO())  # no reconfigure()

    ensure_utf8_output()

    assert utf8.encoding == "UTF8"
