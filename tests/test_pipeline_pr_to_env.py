"""Unit tests for the pr_to_env pipeline.

Pure-Python bits only — real Docker runs are covered by manual end-to-end.
Focus areas:
  * URL parsing (github.com/*/pull/N + gitlab.com MR)
  * URL-file reading (comment stripping)
  * Single-repo enforcement
  * Ledger writing shape
  * Pipeline registers on the Protocol
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repo2rlenv.pipelines.pr_to_env import (
    PrToEnvPipeline,
    UrlParseError,
    _leak_grep_v2,
    _pyproject_sanitize_snippet,
    parse_pr_url,
    read_urls_file,
)


class TestParsePrUrl:
    def test_github_pull(self):
        assert parse_pr_url("https://github.com/huggingface/peft/pull/3083") == (
            "github.com",
            "huggingface",
            "peft",
            3083,
        )

    def test_github_pull_trailing_slash(self):
        assert parse_pr_url("https://github.com/huggingface/peft/pull/3083/") == (
            "github.com",
            "huggingface",
            "peft",
            3083,
        )

    def test_gitlab_mr(self):
        assert parse_pr_url("https://gitlab.com/foo/bar/-/merge_requests/42") == (
            "gitlab.com",
            "foo",
            "bar",
            42,
        )

    def test_http_variant(self):
        assert parse_pr_url("http://github.com/a/b/pull/1")[0] == "github.com"

    def test_rejects_issue_url(self):
        with pytest.raises(UrlParseError):
            parse_pr_url("https://github.com/huggingface/peft/issues/3083")

    def test_rejects_bare_repo(self):
        with pytest.raises(UrlParseError):
            parse_pr_url("https://github.com/huggingface/peft")

    def test_rejects_random(self):
        with pytest.raises(UrlParseError):
            parse_pr_url("not-a-url")


class TestReadUrlsFile:
    def test_reads_one_per_line(self, tmp_path: Path):
        p = tmp_path / "urls.txt"
        p.write_text(
            "https://github.com/huggingface/peft/pull/1\n"
            "https://github.com/huggingface/peft/pull/2\n"
        )
        assert read_urls_file(p) == [
            "https://github.com/huggingface/peft/pull/1",
            "https://github.com/huggingface/peft/pull/2",
        ]

    def test_strips_comments_and_blanks(self, tmp_path: Path):
        p = tmp_path / "urls.txt"
        p.write_text(
            "# header\n"
            "\n"
            "https://github.com/huggingface/peft/pull/1  # inline\n"
            "   \n"
            "https://github.com/huggingface/peft/pull/2\n"
        )
        assert read_urls_file(p) == [
            "https://github.com/huggingface/peft/pull/1",
            "https://github.com/huggingface/peft/pull/2",
        ]


class TestPipelineProtocol:
    def test_has_required_class_attrs(self):
        assert hasattr(PrToEnvPipeline, "name")
        assert hasattr(PrToEnvPipeline, "requires_bootstrap")
        assert PrToEnvPipeline.requires_bootstrap is True
        # Should be marked experimental while gates are landing.
        assert getattr(PrToEnvPipeline, "experimental", False) is True

    def test_is_registered(self):
        from repo2rlenv.pipelines import PIPELINES

        assert "pr_to_env" in PIPELINES


class TestLeakGrepV2:
    def test_strips_short_sha(self):
        text = "Fixed in abcdef1234 and also see deadbeef99"
        out, warns = _leak_grep_v2(text, [], [])
        assert "abcdef1234" not in out
        assert "deadbeef99" not in out
        assert warns == []

    def test_strips_pytest_nodeid(self):
        text = "Run tests/foo/test_bar.py::test_baz to verify"
        out, _ = _leak_grep_v2(text, [], [])
        assert "tests/foo/test_bar.py" not in out
        assert "test_baz" not in out

    def test_flags_basename_soft(self):
        text = "The bug is in the parser.py handling"
        out, warns = _leak_grep_v2(text, ["src/mod/parser.py"], [])
        # Not stripped, just flagged.
        assert "parser.py" in out
        assert any("parser.py" in w for w in warns)

    def test_flags_dirname_soft(self):
        text = "See the linalg module for context"
        out, warns = _leak_grep_v2(text, ["src/linalg/matrix.py"], [])
        assert "linalg" in out
        assert any("linalg" in w for w in warns)

    def test_ignores_short_hex_words(self):
        # "abc123" is only 6 chars — below the 8-char short-SHA threshold.
        text = "code abc123 remains untouched"
        out, _ = _leak_grep_v2(text, [], [])
        assert "abc123" in out

    def test_no_hits_returns_input(self):
        text = "This is a bug where the handler skips validation."
        out, warns = _leak_grep_v2(text, [], [])
        assert out == text
        assert warns == []


class TestPyprojectSanitize:
    def test_snippet_contains_pytest_check(self):
        snippet = _pyproject_sanitize_snippet()
        assert "[tool.pytest]" in snippet
        assert "[tool.pytest.ini_options]" in snippet
        # Must be a runnable RUN block ending PY heredoc.
        assert "RUN python" in snippet
        assert "'PY'" in snippet

    def test_regex_strips_bare_section(self):
        # Simulate the sanitize logic outside Docker.
        import re

        text = (
            "[tool.other]\nfoo = 1\n\n"
            "[tool.pytest]\naddopts = '--foo'\n\n"
            "[tool.pytest.ini_options]\ntestpaths = ['tests']\n"
        )
        cleaned = re.sub(
            r"^\[tool\.pytest\](?![\.\w]).*?(?=^\[|\Z)",
            "",
            text,
            count=1,
            flags=re.MULTILINE | re.DOTALL,
        )
        # The bare section is gone, but ini_options survives.
        assert "[tool.pytest]\naddopts" not in cleaned
        assert "[tool.pytest.ini_options]" in cleaned
        assert "[tool.other]" in cleaned


def test_ledger_shape(tmp_path: Path, monkeypatch):
    """_append_ledger writes one JSONL line per call with the expected fields."""
    # Build a minimal instance skipping __init__ (needs BootstrapResult).
    inst = PrToEnvPipeline.__new__(PrToEnvPipeline)
    inst._append_ledger(
        out_dir=tmp_path,
        slug="huggingface__peft-3083",
        pr_url="https://github.com/huggingface/peft/pull/3083",
        status="keeper",
        reward=1.0,
        f2p_count=5,
        p2p_count=7,
    )
    ledger = tmp_path / "keepers.jsonl"
    assert ledger.exists()
    entry = json.loads(ledger.read_text().strip())
    assert entry["slug"] == "huggingface__peft-3083"
    assert entry["status"] == "keeper"
    assert entry["reward"] == 1.0
    assert entry["f2p_count"] == 5
    assert entry["p2p_count"] == 7
    assert "timestamp" in entry
