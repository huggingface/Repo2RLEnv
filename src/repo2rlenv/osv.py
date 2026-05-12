"""OSV (Open Source Vulnerabilities) API client.

OSV is Google's public vuln database (https://osv.dev) — no auth required,
covers PyPI / npm / crates.io / Go / Maven / NuGet / Debian / Alpine / etc.

We use it to power the `cve_patches` pipeline: query → extract fix commit
URLs from `references[]` → map to a (base_commit, patch) pair via the
standard `gh api repos/.../commits/<sha>` route.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


OSV_QUERY_URL = "https://api.osv.dev/v1/query"

# A `references[].url` that points at a fix commit. We accept either:
#   https://github.com/<owner>/<repo>/commit/<sha40>
#   https://github.com/<owner>/<repo>/pull/<n>/commits/<sha40>
_GH_COMMIT_RE = re.compile(
    r"https?://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/(?:commit|pull/\d+/commits)/(?P<sha>[a-f0-9]{7,40})\b"
)


@dataclass(slots=True)
class OSVVuln:
    """One vulnerability record, distilled from the OSV JSON shape."""

    id: str  # primary identifier (GHSA-* or PYSEC-* or CVE-* etc.)
    aliases: list[str] = field(default_factory=list)  # other ids (often a CVE)
    summary: str = ""
    details: str = ""
    severity_text: str = ""  # human-readable: "LOW" / "MEDIUM" / "HIGH" / "CRITICAL"
    cwe_ids: list[str] = field(default_factory=list)
    published: str = ""  # ISO8601
    references: list[dict[str, str]] = field(default_factory=list)  # raw

    @property
    def cve_id(self) -> str | None:
        """First CVE-* identifier in id or aliases."""
        for cand in [self.id, *self.aliases]:
            if cand.upper().startswith("CVE-"):
                return cand
        return None

    def fix_commits(self, *, owner: str, repo: str) -> list[str]:
        """Return commit SHAs in `references[]` that point at <owner>/<repo>.

        Filters by owner/repo so we don't accept fix URLs from forks /
        unrelated repos (OSV sometimes lists multiple linked projects).
        """
        out: list[str] = []
        for ref in self.references:
            url = ref.get("url", "")
            m = _GH_COMMIT_RE.search(url)
            if not m:
                continue
            if m.group("owner").lower() != owner.lower():
                continue
            if m.group("repo").lower() != repo.lower():
                continue
            sha = m.group("sha")
            if sha not in out:
                out.append(sha)
        return out


def _from_raw(raw: dict) -> OSVVuln:
    db = raw.get("database_specific", {}) or {}
    severity_text = db.get("severity", "") or ""
    if not severity_text:
        # Some records use `severity[]` with CVSS score arrays — fall back to "UNKNOWN"
        sev = raw.get("severity", []) or []
        if sev and isinstance(sev[0], dict):
            severity_text = sev[0].get("type", "")  # e.g. "CVSS_V3"
    return OSVVuln(
        id=raw.get("id", ""),
        aliases=list(raw.get("aliases", []) or []),
        summary=raw.get("summary", "") or "",
        details=raw.get("details", "") or "",
        severity_text=severity_text.upper(),
        cwe_ids=list(db.get("cwe_ids", []) or []),
        published=raw.get("published", "") or "",
        references=list(raw.get("references", []) or []),
    )


class OSVError(RuntimeError):
    pass


def query_vulns(
    package: str,
    ecosystem: str,
    *,
    timeout: float = 30.0,
) -> list[OSVVuln]:
    """POST to OSV `/v1/query` and return the parsed list of vulns.

    `ecosystem` examples: "PyPI", "npm", "crates.io", "Go", "Maven",
    "NuGet", "Debian", "Alpine".
    """
    payload = json.dumps({"package": {"name": package, "ecosystem": ecosystem}}).encode()
    req = urllib.request.Request(
        OSV_QUERY_URL,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise OSVError(f"OSV HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise OSVError(f"OSV network error: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise OSVError(f"OSV returned malformed JSON: {exc}") from exc
    vulns = data.get("vulns") or []
    return [_from_raw(v) for v in vulns]


# Lookup tables for repo → (ecosystem, package) auto-detection.
# These are heuristics — users can always override via pipeline options.
_KNOWN_OWNER_ECOSYSTEM: dict[str, str] = {
    "pallets": "PyPI",
    "pypa": "PyPI",
    "pytest-dev": "PyPI",
    "psf": "PyPI",
    "django": "PyPI",
    "fastapi": "PyPI",
    "encode": "PyPI",
    "nodejs": "npm",
    "expressjs": "npm",
    "facebook": "npm",
    "rust-lang": "crates.io",
    "tokio-rs": "crates.io",
}


def guess_ecosystem(owner: str) -> str:
    """Best-effort: map a GitHub owner to a likely OSV ecosystem.

    Falls back to "PyPI" since that's where the bulk of CVE-attached repos
    Repo2RLEnv targets live. The user can always override via the
    `osv_ecosystem` pipeline option.
    """
    return _KNOWN_OWNER_ECOSYSTEM.get(owner.lower(), "PyPI")


_SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "MODERATE": 2, "HIGH": 3, "CRITICAL": 4}


def severity_at_least(vuln: OSVVuln, threshold: str) -> bool:
    """True iff the vuln's severity meets or exceeds `threshold` (case-insensitive).

    Unknown severities are treated as below threshold (conservative).
    """
    threshold_rank = _SEVERITY_RANK.get(threshold.upper(), 0)
    vuln_rank = _SEVERITY_RANK.get(vuln.severity_text.upper(), 0)
    return vuln_rank >= threshold_rank
