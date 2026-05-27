"""OCI Distribution Spec L1-L4 verification probes.

Given a host (e.g. `ghcr.io`) and a namespace (e.g. `huggingface`), this
module makes ~3-5 HTTP calls to confirm whether we can actually push
images there *right now*, without polluting the user's registry.

Levels:

  L1 — GET /v2/             (registry reachability)
  L2 — Bearer-token exchange against the challenge realm (auth resolution)
  L3 — HEAD /v2/<ns>/<probe>/manifests/<tag>   (read access; 404 is pass)
  L4 — POST /v2/<ns>/<probe>/blobs/uploads/    (write access; 202 is pass)
       followed by DELETE to cancel — leaves no garbage.

Per-registry quirks are handled inline (ECR/GCP-AR repos must pre-exist,
GHCR uses GitHub PATs as bearer credentials, ACR uses 3h tokens).

Reference:
- OCI Distribution Spec: https://github.com/opencontainers/distribution-spec
"""

from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Literal

from repo2rlenv.registry.auth import (
    CredentialSource,
    RegistryAuth,
    RegistryKind,
)

logger = logging.getLogger(__name__)


# Probe image name used for read/write checks. Per the OCI spec a HEAD on a
# nonexistent manifest returns 404 (read access verified); a POST blob upload
# session can be opened then cancelled with no artifact created.
PROBE_IMAGE = "r2e-bootstrap-probe"
PROBE_TAG = "latest"

DEFAULT_TIMEOUT_SEC = 10.0


class ProbeError(Exception):
    """Raised when probing hits an unrecoverable client-side error (not 4xx/5xx)."""


@dataclass(slots=True)
class ProbeResult:
    host: str
    kind: RegistryKind
    namespace: str
    levels_checked: tuple[int, ...]
    reachable: bool = False
    authenticated: bool = False
    can_read: bool = False
    can_write: bool = False
    error: str | None = None
    helper_hint: str | None = None
    elapsed_sec: float = 0.0
    details: dict[int, str] = field(default_factory=dict)

    @property
    def is_pushable(self) -> bool:
        """All four levels passed — registry is ready for push."""
        return self.reachable and self.authenticated and self.can_read and self.can_write


# Header parsing for WWW-Authenticate: Bearer realm=...,service=...,scope=...
_BEARER_KV_RE = re.compile(r'(\w+)="([^"]*)"')


def _parse_bearer_challenge(www_authenticate: str) -> dict[str, str] | None:
    """Extract realm/service/scope from a `Bearer ...` WWW-Authenticate header.

    Returns None if the header isn't a Bearer challenge (e.g. Basic).
    """
    if not www_authenticate:
        return None
    s = www_authenticate.strip()
    if not s.lower().startswith("bearer "):
        return None
    pairs = dict(_BEARER_KV_RE.findall(s[len("Bearer ") :]))
    return pairs or None


def _http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Tiny urllib wrapper that never raises on 4xx/5xx.

    Returns (status, lowercased-headers, body-bytes). Uses urllib (stdlib)
    rather than httpx to avoid an extra runtime dep.
    """
    req = urllib.request.Request(url, method=method, data=body)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (
                resp.status,
                {k.lower(): v for k, v in resp.headers.items()},
                resp.read(),
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            {k.lower(): v for k, v in (exc.headers or {}).items()},
            exc.read() or b"",
        )
    except urllib.error.URLError as exc:
        raise ProbeError(f"network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ProbeError(f"timeout after {timeout}s") from exc


def _resolve_credential(auth: RegistryAuth, host: str) -> tuple[str, str] | None:
    """Resolve (username, password/token) for `host` via the right cred source.

    Returns None if the credential can't be resolved (helper missing, etc.) —
    the probe will then degrade to anonymous-only behaviour.
    """
    if auth.cred_source is CredentialSource.INLINE:
        # The inline path requires reading the original config.json again —
        # we do it lazily here to keep the public surface small.
        from repo2rlenv.registry.auth import _config_path

        cfg = _config_path()
        if not cfg.is_file():
            return None
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        entry = (data.get("auths") or {}).get(host, {})
        if not isinstance(entry, dict):
            return None
        blob = (entry.get("auth") or "").strip()
        if not blob:
            return None
        try:
            decoded = base64.b64decode(blob).decode("utf-8", errors="replace")
        except (ValueError, UnicodeDecodeError):
            return None
        if ":" not in decoded:
            return None
        user, _, password = decoded.partition(":")
        return (user, password)

    helper = auth.helper
    if not helper:
        # CREDSTORE without a helper name shouldn't happen, but be defensive.
        return None
    return _docker_credential_helper_get(helper, host)


def _docker_credential_helper_get(helper: str, host: str) -> tuple[str, str] | None:
    """Shell out to `docker-credential-<helper> get` for `host`.

    Returns (username, password) or None if the helper doesn't recognize the
    host (some helpers like `desktop` emit `credentials not found` on stderr
    and exit non-zero).
    """
    binary = f"docker-credential-{helper}"
    try:
        proc = subprocess.run(
            [binary, "get"],
            input=host,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return None
    user = payload.get("Username", "")
    secret = payload.get("Secret", "")
    if not secret:
        return None
    # AWS/GCloud helpers sometimes return Username="AWS" / "_dcgcloud_token"
    # and the secret IS the bearer token; we treat user as username, secret
    # as password, and let the token-exchange step decide.
    return (user or "", secret)


# --------------------------------------------------------------------------
# L1 — base endpoint reachability
# --------------------------------------------------------------------------


def _l1_reachable(host: str, timeout: float) -> tuple[bool, str | None, str | None]:
    """Return (ok, error, www_authenticate_header).

    A 200 or 401 both indicate "registry is up." Anything else (DNS / refused
    connection / 5xx / timeout) means we can't proceed.
    """
    scheme = "http" if _is_local_host(host) else "https"
    url = f"{scheme}://{host}/v2/"
    try:
        status, headers, _ = _http_request("GET", url, timeout=timeout)
    except ProbeError as exc:
        return (False, str(exc), None)
    if status in (200, 401):
        return (True, None, headers.get("www-authenticate"))
    return (False, f"unexpected status {status} from /v2/", headers.get("www-authenticate"))


def _is_local_host(host: str) -> bool:
    return host in {"localhost", "127.0.0.1"} or host.startswith(("localhost:", "127.0.0.1:"))


# --------------------------------------------------------------------------
# L2 — auth resolution / Bearer token exchange
# --------------------------------------------------------------------------


def _l2_token_exchange(
    challenge: dict[str, str] | None,
    credential: tuple[str, str] | None,
    *,
    namespace: str,
    timeout: float,
) -> tuple[bool, str | None, str | None, list[str]]:
    """Exchange (user, pass) for a Bearer token at `challenge.realm`.

    Returns (ok, error, bearer_token, granted_scopes).

    granted_scopes is the list of scope strings the token server actually
    granted — typically ["repository:<ns>/<probe>:pull,push"] or a subset.
    We use this to distinguish read-only from read+write tokens.
    """
    if challenge is None:
        # No challenge means the registry doesn't require auth (e.g. local
        # registry:2 without htpasswd). Treat as "authenticated as anonymous";
        # subsequent calls just don't add an Authorization header.
        return (True, None, None, [])

    realm = challenge.get("realm")
    service = challenge.get("service", "")
    if not realm:
        return (False, "challenge missing realm", None, [])

    scope = f"repository:{namespace}/{PROBE_IMAGE}:pull,push"
    query = []
    if service:
        query.append(f"service={service}")
    query.append(f"scope={scope}")
    url = f"{realm}?{'&'.join(query)}"

    headers: dict[str, str] = {"Accept": "application/json"}
    if credential is not None:
        user, password = credential
        basic = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {basic}"

    try:
        status, _, body = _http_request("GET", url, headers=headers, timeout=timeout)
    except ProbeError as exc:
        return (False, str(exc), None, [])
    if status != 200:
        return (False, f"token server returned {status}", None, [])
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return (False, "token server returned non-JSON", None, [])
    token = payload.get("token") or payload.get("access_token")
    if not token:
        return (False, "token server returned no token", None, [])
    # GHCR returns `scope` as a string with comma-separated entries; ACR and
    # others may return a list. Normalize.
    raw_scope = payload.get("scope", scope)
    granted = [str(s) for s in raw_scope] if isinstance(raw_scope, list) else [str(raw_scope)]
    return (True, None, token, granted)


def _has_push_scope(granted_scopes: list[str]) -> bool:
    """True if any granted scope string includes `push`."""
    return any("push" in s for s in granted_scopes)


# --------------------------------------------------------------------------
# L3 — read probe (HEAD manifest)
# --------------------------------------------------------------------------


def _l3_can_read(
    host: str,
    namespace: str,
    bearer: str | None,
    timeout: float,
) -> tuple[str, str | None]:
    """HEAD /v2/<ns>/<probe>/manifests/<tag>.

    Returns (status, err) where status is:
      * "ok"           — 200/404: namespace reachable + readable.
      * "inconclusive" — 401/403: AMBIGUOUS. Docker Hub (and some others)
                         return 401 for a nonexistent/private repo even with
                         valid push creds, so a 401 here does NOT mean "no
                         access". The L4 write probe is authoritative.
      * "fail"         — anything else (real problem).
    """
    scheme = "http" if _is_local_host(host) else "https"
    url = f"{scheme}://{host}/v2/{namespace}/{PROBE_IMAGE}/manifests/{PROBE_TAG}"
    headers: dict[str, str] = {
        "Accept": (
            "application/vnd.oci.image.manifest.v1+json,"
            "application/vnd.docker.distribution.manifest.v2+json"
        ),
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        status, _, _ = _http_request("HEAD", url, headers=headers, timeout=timeout)
    except ProbeError as exc:
        return ("fail", str(exc))
    if status in (200, 404):
        return ("ok", None)
    if status in (401, 403):
        return ("inconclusive", f"read ambiguous ({status}); deferring to write probe")
    return ("fail", f"unexpected status {status}")


# --------------------------------------------------------------------------
# L4 — write probe (POST blob upload + DELETE cancel)
# --------------------------------------------------------------------------


def _l4_can_write(
    host: str,
    namespace: str,
    bearer: str | None,
    timeout: float,
) -> tuple[bool, str | None]:
    """POST /v2/<ns>/<probe>/blobs/uploads/; 202 = write OK, then cancel.

    Per OCI spec, 202 returns a Location header pointing at the upload
    session URL — we DELETE that immediately to leave nothing behind.
    """
    scheme = "http" if _is_local_host(host) else "https"
    url = f"{scheme}://{host}/v2/{namespace}/{PROBE_IMAGE}/blobs/uploads/"
    headers: dict[str, str] = {"Content-Length": "0"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        status, resp_headers, _ = _http_request(
            "POST", url, headers=headers, timeout=timeout, body=b""
        )
    except ProbeError as exc:
        return (False, str(exc))
    if status == 202:
        # Cancel the session — best-effort, don't fail the probe if cancel fails.
        location = resp_headers.get("location")
        if location:
            cancel_url = location if location.startswith("http") else f"{scheme}://{host}{location}"
            try:
                _http_request("DELETE", cancel_url, headers=headers, timeout=timeout)
            except ProbeError:
                logger.debug("probe upload-cancel failed; harmless")
        return (True, None)
    if status in (401, 403):
        return (False, f"write denied ({status})")
    if status == 404:
        # ECR / GCP AR: repository must pre-exist. We classify this as
        # "write not yet possible" — caller can decide whether to attempt
        # repo creation via the vendor API.
        return (False, "repository does not exist (pre-create required)")
    return (False, f"unexpected status {status}")


# --------------------------------------------------------------------------
# Public probe entry point
# --------------------------------------------------------------------------


def probe(
    auth: RegistryAuth,
    namespace: str,
    *,
    levels: tuple[int, ...] = (1, 2, 3, 4),
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> ProbeResult:
    """Run the L1-L4 verification protocol against `auth.host` for `namespace`.

    `namespace` is the registry path component (e.g. `huggingface` for GHCR,
    `r2e` for ECR Public, `myproject/myrepo` for GCP AR).

    Returns a populated `ProbeResult`. Never raises for protocol-level
    failures (4xx, 5xx, denied) — only for client-side network errors that
    prevent the probe entirely.
    """
    started = time.monotonic()
    result = ProbeResult(
        host=auth.host,
        kind=auth.kind,
        namespace=namespace,
        levels_checked=levels,
    )

    # L1
    if 1 in levels:
        ok, err, www_auth = _l1_reachable(auth.host, timeout)
        result.reachable = ok
        if not ok:
            result.error = err or "L1 unreachable"
            result.elapsed_sec = round(time.monotonic() - started, 3)
            return result
        result.details[1] = "reachable"
    else:
        result.reachable = True
        www_auth = None

    challenge = _parse_bearer_challenge(www_auth) if www_auth else None

    bearer: str | None = None
    granted: list[str] = []

    # L2
    if 2 in levels:
        credential = _resolve_credential(auth, auth.host)
        ok, err, bearer, granted = _l2_token_exchange(
            challenge, credential, namespace=namespace, timeout=timeout
        )
        result.authenticated = ok
        if not ok:
            result.error = err
            # Helper hint for common cases.
            if auth.kind is RegistryKind.GHCR:
                result.helper_hint = (
                    "gh auth refresh -h github.com -s write:packages && "
                    'echo "$(gh auth token)" | docker login ghcr.io '
                    "-u $(gh api user --jq .login) --password-stdin"
                )
            elif auth.kind is RegistryKind.ECR_PRIVATE:
                result.helper_hint = (
                    "aws ecr get-login-password --region <REGION> | "
                    f"docker login --username AWS --password-stdin {auth.host}"
                )
            elif auth.kind is RegistryKind.ACR:
                result.helper_hint = f"az acr login --name {auth.host.split('.')[0]}"
            elif auth.kind is RegistryKind.GCP_AR:
                result.helper_hint = f"gcloud auth configure-docker {auth.host}"
            elif auth.kind is RegistryKind.DOCKER_HUB:
                result.helper_hint = "docker login"
            result.elapsed_sec = round(time.monotonic() - started, 3)
            return result
        result.details[2] = "authenticated"

    # L3
    if 3 in levels:
        status, err = _l3_can_read(auth.host, namespace, bearer, timeout)
        result.can_read = status == "ok"
        result.details[3] = "read OK" if status == "ok" else (err or "read failed")
        # Only a hard "fail" stops here. An "inconclusive" 401/403 (e.g. Docker
        # Hub on a nonexistent repo) falls through to the authoritative L4
        # write probe — write access implies the namespace is usable.
        if status == "fail":
            result.error = err
            result.elapsed_sec = round(time.monotonic() - started, 3)
            return result

    # L4
    if 4 in levels:
        # Cheap pre-check: if L2 granted only `pull`, we already know we
        # can't write. Skip the round-trip.
        if 2 in levels and granted and not _has_push_scope(granted):
            result.can_write = False
            result.error = "token lacks push scope"
            result.details[4] = "push scope not granted"
            result.elapsed_sec = round(time.monotonic() - started, 3)
            return result
        ok, err = _l4_can_write(auth.host, namespace, bearer, timeout)
        result.can_write = ok
        result.details[4] = "write OK" if ok else (err or "write failed")
        if ok and not result.can_read:
            # Write access implies the namespace is usable (after a push the
            # repo exists + is readable). Clears an inconclusive L3.
            result.can_read = True
            result.details[3] = "read OK (inferred from write access)"
        if not ok:
            result.error = err

    result.elapsed_sec = round(time.monotonic() - started, 3)
    return result


def select_best(
    results: list[ProbeResult],
    *,
    require_write: bool = True,
) -> ProbeResult | None:
    """Pick the highest-ranked pushable result.

    Preference: GHCR > GCP_AR > ECR_PUBLIC > ECR_PRIVATE > ACR > LOCAL > DOCKER_HUB.
    Rationale: ranked by anonymous-pull friendliness for public images.
    Docker Hub is last because of the 100/6h anonymous rate limit.
    """
    rank: dict[RegistryKind, int] = {
        RegistryKind.GHCR: 0,
        RegistryKind.GCP_AR: 1,
        RegistryKind.ECR_PUBLIC: 2,
        RegistryKind.ECR_PRIVATE: 3,
        RegistryKind.ACR: 4,
        RegistryKind.LOCAL: 5,
        RegistryKind.DOCKER_HUB: 6,
    }
    eligible = [r for r in results if (r.is_pushable if require_write else r.authenticated)]
    if not eligible:
        return None
    return min(eligible, key=lambda r: rank.get(r.kind, 99))


# Public API surface
__all__ = [
    "DEFAULT_TIMEOUT_SEC",
    "PROBE_IMAGE",
    "PROBE_TAG",
    "ProbeError",
    "ProbeResult",
    "probe",
    "select_best",
]


_LevelInt = Literal[1, 2, 3, 4]
