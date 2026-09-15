from __future__ import annotations

import json

import pytest

from repo2rlenv.pipelines.recipes.terminalworld import discovery


def test_discovery_uses_only_recording_links_and_reuses_completed_pages(tmp_path, monkeypatch):
    reads = []

    def download(client, url, limit):
        reads.append(url)
        if url.endswith("robots.txt"):
            return "User-agent: *\nAllow: /\n"
        if "page=1" in url:
            return '<a href="/a/123">One</a><a href="https://example.com/a/456">External</a><a href="/a/123.cast">Media</a><a href="/a/123">Duplicate</a>'
        return '<a href="https://asciinema.org/a/789">Two</a>'

    monkeypatch.setattr(discovery, "_download", download)
    monkeypatch.setattr(discovery.time, "sleep", lambda _: None)
    ids = discovery.discover_recordings(tmp_path, feeds=["recent"], pages_per_feed=5)
    assert ids == ["123", "789"]
    assert len(reads) == 4  # robots, first, second, repeated third page
    assert json.loads((tmp_path / "ids.json").read_text()) == ids
    assert discovery.discover_recordings(tmp_path, feeds=["recent"], pages_per_feed=5) == ids
    assert len(reads) == 5  # Only robots is refreshed on resume.


def test_discovery_obeys_robots_and_records_the_block(tmp_path, monkeypatch):
    reads = []

    def download(client, url, limit):
        reads.append(url)
        return "User-agent: *\nDisallow: /explore/\n"

    monkeypatch.setattr(discovery, "_download", download)
    monkeypatch.setattr(discovery.time, "sleep", lambda _: None)
    assert discovery.discover_recordings(tmp_path, feeds=["public"]) == []
    assert len(reads) == 1
    record = json.loads((tmp_path / "pages/public-001.json").read_text())
    assert record["error_type"] == "ValueError"
    with pytest.raises(ValueError, match="one to 50"):
        discovery.discover_recordings(tmp_path, feeds=["public"], pages_per_feed=51)


def test_explicit_profiles_stay_on_asciinema_and_respect_the_page_bound(tmp_path, monkeypatch):
    urls = []

    def download(client, url, limit):
        urls.append(url)
        return (
            "User-agent: *\nAllow: /\n"
            if url.endswith("robots.txt")
            else '<a href="/a/42">Record</a>'
        )

    monkeypatch.setattr(discovery, "_download", download)
    monkeypatch.setattr(discovery.time, "sleep", lambda _: None)
    assert discovery.discover_recordings(
        tmp_path, feeds=[], profiles=["/~fixture-user"], pages_per_feed=1
    ) == ["42"]
    assert urls[-1] == "https://asciinema.org/~fixture-user?page=1"
    with pytest.raises(ValueError, match="explicit public"):
        discovery.discover_recordings(tmp_path, feeds=[], profiles=["https://other.example/~user"])
    with pytest.raises(ValueError, match="at most 200"):
        discovery.discover_recordings(
            tmp_path, feeds=[], profiles=[f"/~user{i}" for i in range(5)], pages_per_feed=50
        )
