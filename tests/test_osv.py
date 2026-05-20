"""OSV client + helpers — unit tests.

Network calls are not exercised here (we don't hit api.osv.dev from tests).
We feed canned raw JSON shapes through `_from_raw` and verify parsing,
filter-by-repo behavior, and severity comparisons.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest import mock

import pytest

from repo2rlenv.osv import (
    OSVVuln,
    _cache_key,
    _cache_path,
    _from_raw,
    _read_cache,
    _write_cache,
    guess_ecosystem,
    query_vulns_cached,
    severity_at_least,
)

# ---------------------------------------------------------------------------
# _from_raw
# ---------------------------------------------------------------------------


def test_from_raw_minimal():
    raw = {"id": "GHSA-abc-def-ghi"}
    v = _from_raw(raw)
    assert v.id == "GHSA-abc-def-ghi"
    assert v.aliases == []
    assert v.summary == ""
    assert v.severity_text == ""


def test_from_raw_full():
    raw = {
        "id": "GHSA-29vq-49wr-vm6x",
        "aliases": ["CVE-2026-1234", "PYSEC-2026-1"],
        "summary": "Werkzeug safe_join() bypass",
        "details": "On Windows, the safe_join() function ...",
        "database_specific": {
            "severity": "HIGH",
            "cwe_ids": ["CWE-22"],
        },
        "published": "2026-02-19T00:00:00Z",
        "references": [
            {"type": "ADVISORY", "url": "https://github.com/.../GHSA-29vq-49wr-vm6x"},
            {
                "type": "WEB",
                "url": "https://github.com/pallets/werkzeug/commit/f407712fdc60a09c2b3f4fe7db557703e5d9338d",
            },
        ],
    }
    v = _from_raw(raw)
    assert v.id == "GHSA-29vq-49wr-vm6x"
    assert v.aliases == ["CVE-2026-1234", "PYSEC-2026-1"]
    assert v.severity_text == "HIGH"
    assert v.cwe_ids == ["CWE-22"]


# ---------------------------------------------------------------------------
# OSVVuln.cve_id
# ---------------------------------------------------------------------------


def test_cve_id_from_aliases():
    v = OSVVuln(id="GHSA-x-y-z", aliases=["CVE-2026-1234"])
    assert v.cve_id == "CVE-2026-1234"


def test_cve_id_from_primary_id():
    v = OSVVuln(id="CVE-2024-9999")
    assert v.cve_id == "CVE-2024-9999"


def test_cve_id_none_when_neither_has_cve():
    v = OSVVuln(id="GHSA-x", aliases=["PYSEC-2026-1"])
    assert v.cve_id is None


# ---------------------------------------------------------------------------
# OSVVuln.fix_commits
# ---------------------------------------------------------------------------


def test_fix_commits_filters_by_owner_repo():
    v = OSVVuln(
        id="x",
        references=[
            {"type": "WEB", "url": "https://github.com/pallets/werkzeug/commit/abc1234567890"},
            {"type": "WEB", "url": "https://github.com/fork/werkzeug/commit/feedface1234567"},
            {"type": "ADVISORY", "url": "https://nvd.nist.gov/..."},
        ],
    )
    assert v.fix_commits(owner="pallets", repo="werkzeug") == ["abc1234567890"]


def test_fix_commits_supports_pr_commit_url():
    v = OSVVuln(
        id="x",
        references=[
            {
                "type": "WEB",
                "url": "https://github.com/pallets/werkzeug/pull/1234/commits/abc1234567890",
            },
        ],
    )
    assert v.fix_commits(owner="pallets", repo="werkzeug") == ["abc1234567890"]


def test_fix_commits_deduplicates():
    v = OSVVuln(
        id="x",
        references=[
            {"type": "WEB", "url": "https://github.com/pallets/werkzeug/commit/aaaaaa1234567"},
            {"type": "WEB", "url": "https://github.com/pallets/werkzeug/commit/aaaaaa1234567"},
        ],
    )
    assert v.fix_commits(owner="pallets", repo="werkzeug") == ["aaaaaa1234567"]


def test_fix_commits_empty_when_no_github_commit_refs():
    v = OSVVuln(
        id="x",
        references=[
            {"type": "WEB", "url": "https://example.com/blog"},
            {"type": "ADVISORY", "url": "https://nvd.nist.gov/..."},
        ],
    )
    assert v.fix_commits(owner="pallets", repo="werkzeug") == []


# ---------------------------------------------------------------------------
# severity_at_least
# ---------------------------------------------------------------------------


def test_severity_high_at_least_medium():
    v = OSVVuln(id="x", severity_text="HIGH")
    assert severity_at_least(v, "medium")


def test_severity_low_below_high():
    v = OSVVuln(id="x", severity_text="LOW")
    assert not severity_at_least(v, "high")


def test_severity_critical_at_least_critical():
    v = OSVVuln(id="x", severity_text="CRITICAL")
    assert severity_at_least(v, "critical")


def test_severity_moderate_treated_as_medium():
    v = OSVVuln(id="x", severity_text="MODERATE")
    assert severity_at_least(v, "medium")


def test_severity_unknown_below_threshold():
    v = OSVVuln(id="x", severity_text="")
    assert not severity_at_least(v, "low")


# ---------------------------------------------------------------------------
# guess_ecosystem
# ---------------------------------------------------------------------------


def test_guess_ecosystem_known_pypi():
    assert guess_ecosystem("pallets") == "PyPI"


def test_guess_ecosystem_known_npm():
    assert guess_ecosystem("expressjs") == "npm"


def test_guess_ecosystem_known_crates():
    assert guess_ecosystem("rust-lang") == "crates.io"


def test_guess_ecosystem_unknown_defaults_pypi():
    assert guess_ecosystem("nobody-knows-this") == "PyPI"


def test_guess_ecosystem_case_insensitive():
    assert guess_ecosystem("PALLETS") == "PyPI"


# ---------------------------------------------------------------------------
# OSV cache (v0.8.3 Arc 4)
# ---------------------------------------------------------------------------


def test_cache_key_stable_per_package_ecosystem() -> None:
    assert _cache_key("click", "PyPI") == _cache_key("Click", "pypi")  # case-insensitive
    assert _cache_key("click", "PyPI") != _cache_key("flask", "PyPI")


def test_cache_path_uses_provided_dir(tmp_path: Path) -> None:
    p = _cache_path("click", "PyPI", cache_dir=tmp_path)
    assert p.parent == tmp_path
    assert p.suffix == ".json"


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    p = _cache_path("foo", "PyPI", cache_dir=tmp_path)
    raw = [
        {
            "id": "CVE-2025-9999",
            "aliases": [],
            "summary": "tiny",
            "details": "",
            "database_specific": {"severity": "HIGH"},
            "published": "2025-01-01",
            "references": [],
        }
    ]
    _write_cache(p, raw)
    vulns, fresh = _read_cache(p, ttl_seconds=10_000)
    assert fresh is True
    assert len(vulns) == 1
    assert vulns[0].id == "CVE-2025-9999"
    assert vulns[0].severity_text == "HIGH"


def test_read_cache_expired(tmp_path: Path) -> None:
    p = _cache_path("foo", "PyPI", cache_dir=tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"stamped_at": time.time() - 1000, "vulns": [{"id": "x"}]}))
    vulns, fresh = _read_cache(p, ttl_seconds=100)
    assert fresh is False
    assert vulns == []


def test_read_cache_malformed(tmp_path: Path) -> None:
    p = _cache_path("foo", "PyPI", cache_dir=tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    vulns, fresh = _read_cache(p, ttl_seconds=10_000)
    assert fresh is False
    assert vulns == []


def test_read_cache_missing_file_is_miss(tmp_path: Path) -> None:
    p = tmp_path / "nope.json"
    vulns, fresh = _read_cache(p, ttl_seconds=10_000)
    assert fresh is False
    assert vulns == []


def test_query_vulns_cached_uses_cache_on_hit(tmp_path: Path) -> None:
    """When the cache is fresh, the live query is NOT called."""
    raw = [
        {
            "id": "CVE-2025-1111",
            "aliases": [],
            "summary": "",
            "details": "",
            "database_specific": {"severity": "MEDIUM"},
            "published": "",
            "references": [],
        }
    ]
    p = _cache_path("foo", "PyPI", cache_dir=tmp_path)
    _write_cache(p, raw)

    with mock.patch("repo2rlenv.osv.query_vulns") as m:
        result = query_vulns_cached(
            "foo", "PyPI", cache_enabled=True, ttl_seconds=10_000, cache_dir=tmp_path
        )
    assert m.call_count == 0  # cache hit short-circuited
    assert [v.id for v in result] == ["CVE-2025-1111"]


def test_query_vulns_cached_falls_back_to_live(tmp_path: Path) -> None:
    """When cache is empty, the live query is called once + result is cached."""
    fake_vuln = OSVVuln(id="CVE-2025-2222", severity_text="LOW")
    with mock.patch("repo2rlenv.osv.query_vulns", return_value=[fake_vuln]) as m:
        first = query_vulns_cached(
            "bar", "PyPI", cache_enabled=True, ttl_seconds=10_000, cache_dir=tmp_path
        )
        assert m.call_count == 1
        # Second call: should hit the cache from the first call
        second = query_vulns_cached(
            "bar", "PyPI", cache_enabled=True, ttl_seconds=10_000, cache_dir=tmp_path
        )
        assert m.call_count == 1  # still 1 — cache hit
    assert [v.id for v in first] == ["CVE-2025-2222"]
    assert [v.id for v in second] == ["CVE-2025-2222"]


def test_query_vulns_cached_disabled_always_calls_live(tmp_path: Path) -> None:
    fake = [OSVVuln(id="CVE-X")]
    with mock.patch("repo2rlenv.osv.query_vulns", return_value=fake) as m:
        query_vulns_cached("baz", "PyPI", cache_enabled=False, cache_dir=tmp_path)
        query_vulns_cached("baz", "PyPI", cache_enabled=False, cache_dir=tmp_path)
    assert m.call_count == 2  # cache bypassed both calls


def test_query_vulns_cached_handles_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only cache dir shouldn't crash the pipeline."""

    def fail_write(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("repo2rlenv.osv._write_cache", fail_write)
    fake = [OSVVuln(id="CVE-Y")]
    with mock.patch("repo2rlenv.osv.query_vulns", return_value=fake):
        result = query_vulns_cached("qux", "PyPI", cache_enabled=True, cache_dir=tmp_path)
    assert [v.id for v in result] == ["CVE-Y"]
