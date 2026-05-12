"""OSV client + helpers — unit tests.

Network calls are not exercised here (we don't hit api.osv.dev from tests).
We feed canned raw JSON shapes through `_from_raw` and verify parsing,
filter-by-repo behavior, and severity comparisons.
"""

from __future__ import annotations

from repo2rlenv.osv import (
    OSVVuln,
    _from_raw,
    guess_ecosystem,
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
