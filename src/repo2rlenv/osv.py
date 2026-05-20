"""OSV (Open Source Vulnerabilities) API client.

OSV is Google's public vuln database (https://osv.dev) — no auth required,
covers PyPI / npm / crates.io / Go / Maven / NuGet / Debian / Alpine / etc.

We use it to power the `cve_patches` pipeline: query → extract fix commit
URLs from `references[]` → map to a (base_commit, patch) pair via the
standard `gh api repos/.../commits/<sha>` route.

A simple file-system cache lives under ``$REPO2RLENV_OSV_CACHE_DIR`` (default
``~/.cache/repo2rlenv/osv/``). Cache entries are per ``(package, ecosystem)``
JSON files keyed on a sha1 of the request; entries expire after the
``ttl_seconds`` you pass to :func:`query_vulns_cached`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


OSV_QUERY_URL = "https://api.osv.dev/v1/query"


def _default_cache_dir() -> Path:
    """Filesystem location for the OSV result cache. Env override:
    ``REPO2RLENV_OSV_CACHE_DIR``. Default: ``~/.cache/repo2rlenv/osv/``.
    """
    if env := os.environ.get("REPO2RLENV_OSV_CACHE_DIR"):
        return Path(env)
    return Path.home() / ".cache" / "repo2rlenv" / "osv"


def _cache_key(package: str, ecosystem: str) -> str:
    raw = f"{ecosystem.lower()}::{package.lower()}".encode()
    return hashlib.sha1(raw).hexdigest()


def _cache_path(package: str, ecosystem: str, *, cache_dir: Path | None = None) -> Path:
    base = cache_dir if cache_dir is not None else _default_cache_dir()
    return base / f"{_cache_key(package, ecosystem)}.json"


def _read_cache(path: Path, *, ttl_seconds: int) -> tuple[list[OSVVuln], bool]:
    """Return (vulns, fresh): fresh=True only if the file exists AND
    is younger than ``ttl_seconds``.
    """
    if not path.exists():
        return [], False
    try:
        raw = json.loads(path.read_text())
        stamped_at = float(raw.get("stamped_at", 0))
        if time.time() - stamped_at > ttl_seconds:
            return [], False
        return [_from_raw(v) for v in raw.get("vulns", [])], True
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        # Malformed cache — treat as miss; the caller will refresh.
        return [], False


def _write_cache(path: Path, vulns: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"stamped_at": time.time(), "vulns": vulns}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


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


def query_vulns_cached(
    package: str,
    ecosystem: str,
    *,
    timeout: float = 30.0,
    cache_enabled: bool = True,
    ttl_seconds: int = 7 * 24 * 3600,
    cache_dir: Path | None = None,
) -> list[OSVVuln]:
    """Cache-aware variant of :func:`query_vulns`.

    On cache hit (file exists + younger than ``ttl_seconds``) the entry is
    returned without hitting the network. On cache miss we run the live
    query, store the raw JSON, and return the parsed list. Disable caching
    entirely with ``cache_enabled=False``.
    """
    if not cache_enabled:
        return query_vulns(package, ecosystem, timeout=timeout)

    path = _cache_path(package, ecosystem, cache_dir=cache_dir)
    vulns, fresh = _read_cache(path, ttl_seconds=ttl_seconds)
    if fresh:
        logger.info("osv cache hit: %s/%s (%d vulns from %s)", ecosystem, package, len(vulns), path)
        return vulns

    fetched = query_vulns(package, ecosystem, timeout=timeout)
    # Store the raw shapes so re-parsing is forward-compatible with
    # OSVVuln field additions.
    raw_payload = [
        {
            "id": v.id,
            "aliases": v.aliases,
            "summary": v.summary,
            "details": v.details,
            "database_specific": {
                "severity": v.severity_text,
                "cwe_ids": v.cwe_ids,
            },
            "published": v.published,
            "references": v.references,
        }
        for v in fetched
    ]
    try:
        _write_cache(path, raw_payload)
    except OSError as exc:
        logger.warning("osv cache write failed (%s) — proceeding without cache", exc)
    return fetched


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
