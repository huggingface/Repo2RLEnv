"""Tests for `registry.probe` — OCI L1-L4 verification.

We mock the urllib transport via a controllable fake so every protocol
path is exercised without network access. A separate live test (gated on
$RUN_LIVE_GHCR / $GHCR_TEST_TOKEN) exists for end-to-end verification.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from repo2rlenv.registry import probe as probe_mod
from repo2rlenv.registry.auth import (
    CredentialSource,
    RegistryAuth,
    RegistryKind,
)
from repo2rlenv.registry.probe import (
    ProbeResult,
    _has_push_scope,
    _parse_bearer_challenge,
    probe,
    select_best,
)

# --------------------------------------------------------------------------
# Tiny HTTP fake — records calls; emits scripted responses per (method, url).
# --------------------------------------------------------------------------


class _FakeHTTP:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []
        self.script: dict[tuple[str, str], list[tuple[int, dict[str, str], bytes]]] = {}

    def respond(
        self,
        method: str,
        url: str,
        status: int,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        """Queue a response. Multiple queued responses for the same key are
        consumed in order."""
        self.script.setdefault((method, url), []).append((status, headers or {}, body))

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = probe_mod.DEFAULT_TIMEOUT_SEC,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        self.calls.append((method, url, headers or {}, body))
        queue = self.script.get((method, url))
        if not queue:
            # Default to 404 when unscripted, with a readable message
            return (404, {}, f"unscripted: {method} {url}".encode())
        return queue.pop(0)


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> _FakeHTTP:
    fake = _FakeHTTP()
    monkeypatch.setattr(probe_mod, "_http_request", fake)
    return fake


# --------------------------------------------------------------------------
# Header parsing
# --------------------------------------------------------------------------


class TestParseChallenge:
    def test_ghcr_challenge(self) -> None:
        out = _parse_bearer_challenge('Bearer realm="https://ghcr.io/token",service="ghcr.io"')
        assert out == {"realm": "https://ghcr.io/token", "service": "ghcr.io"}

    def test_with_scope(self) -> None:
        out = _parse_bearer_challenge(
            'Bearer realm="https://auth.docker.io/token",'
            'service="registry.docker.io",'
            'scope="repository:library/alpine:pull"'
        )
        assert out is not None
        assert out["realm"] == "https://auth.docker.io/token"
        assert out["scope"] == "repository:library/alpine:pull"

    def test_basic_challenge_returns_none(self) -> None:
        assert _parse_bearer_challenge('Basic realm="registry"') is None

    def test_empty_returns_none(self) -> None:
        assert _parse_bearer_challenge("") is None
        assert _parse_bearer_challenge(None) is None  # type: ignore[arg-type]


class TestPushScope:
    def test_pull_push_granted(self) -> None:
        assert _has_push_scope(["repository:foo/bar:pull,push"]) is True

    def test_pull_only(self) -> None:
        assert _has_push_scope(["repository:foo/bar:pull"]) is False

    def test_empty(self) -> None:
        assert _has_push_scope([]) is False


# --------------------------------------------------------------------------
# Full probe scenarios
# --------------------------------------------------------------------------


def _make_auth(host: str = "ghcr.io", kind: RegistryKind = RegistryKind.GHCR) -> RegistryAuth:
    return RegistryAuth(
        host=host,
        kind=kind,
        cred_source=CredentialSource.INLINE,
        helper=None,
    )


def _script_happy_path(fake: _FakeHTTP, host: str, namespace: str) -> None:
    """Convenience: scripted L1-L4 all-pass."""
    fake.respond(
        "GET",
        f"https://{host}/v2/",
        401,
        headers={"www-authenticate": f'Bearer realm="https://{host}/token",service="{host}"'},
    )
    # L2 token exchange
    fake.respond(
        "GET",
        f"https://{host}/token?service={host}&scope=repository:{namespace}/r2e-bootstrap-probe:pull,push",
        200,
        body=json.dumps(
            {
                "token": "abc.def.ghi",
                "scope": f"repository:{namespace}/r2e-bootstrap-probe:pull,push",
            }
        ).encode(),
    )
    # L3 read
    fake.respond(
        "HEAD",
        f"https://{host}/v2/{namespace}/r2e-bootstrap-probe/manifests/latest",
        404,
    )
    # L4 write — start upload
    fake.respond(
        "POST",
        f"https://{host}/v2/{namespace}/r2e-bootstrap-probe/blobs/uploads/",
        202,
        headers={
            "location": f"/v2/{namespace}/r2e-bootstrap-probe/blobs/uploads/sess-uuid",
        },
    )
    # L4 cancel
    fake.respond(
        "DELETE",
        f"https://{host}/v2/{namespace}/r2e-bootstrap-probe/blobs/uploads/sess-uuid",
        204,
    )


def _inline_auth_config(tmp_path, monkeypatch, host: str = "ghcr.io") -> None:
    """Install a fake config.json with inline creds for `host`."""
    import base64

    cfg = tmp_path / ".docker" / "config.json"
    cfg.parent.mkdir(parents=True)
    blob = base64.b64encode(b"user:pass").decode()
    cfg.write_text(json.dumps({"auths": {host: {"auth": blob}}}))
    monkeypatch.setenv("DOCKER_CONFIG", str(cfg.parent))


class TestProbeHappyPath:
    def test_ghcr_all_levels_pass(
        self, fake_http: _FakeHTTP, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _inline_auth_config(tmp_path, monkeypatch)
        _script_happy_path(fake_http, "ghcr.io", "huggingface")
        result = probe(_make_auth(), "huggingface")
        assert result.is_pushable, f"failed: {result}"
        assert result.reachable
        assert result.authenticated
        assert result.can_read
        assert result.can_write
        assert result.error is None


class TestL1Reachability:
    def test_dns_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise probe_mod.ProbeError("network unreachable")

        monkeypatch.setattr(probe_mod, "_http_request", boom)
        result = probe(_make_auth(), "huggingface", levels=(1,))
        assert not result.reachable
        assert "unreachable" in (result.error or "")

    def test_anonymous_local_registry(
        self, fake_http: _FakeHTTP, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Local registry:2 with no auth → no challenge, no token, but probes pass."""
        host = "localhost:5000"
        # L1 — 200, no auth required
        fake_http.respond("GET", f"http://{host}/v2/", 200, headers={})
        # L3 — 404 (probe image doesn't exist)
        fake_http.respond(
            "HEAD",
            f"http://{host}/v2/r2e-test/r2e-bootstrap-probe/manifests/latest",
            404,
        )
        # L4 — 202, then DELETE
        fake_http.respond(
            "POST",
            f"http://{host}/v2/r2e-test/r2e-bootstrap-probe/blobs/uploads/",
            202,
            headers={"location": "/v2/r2e-test/r2e-bootstrap-probe/blobs/uploads/u1"},
        )
        fake_http.respond(
            "DELETE",
            f"http://{host}/v2/r2e-test/r2e-bootstrap-probe/blobs/uploads/u1",
            204,
        )
        auth = RegistryAuth(host, RegistryKind.LOCAL, CredentialSource.EMPTY)
        result = probe(auth, "r2e-test")
        assert result.is_pushable, result.details


class TestL2AuthFailure:
    def test_expired_token(
        self, fake_http: _FakeHTTP, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _inline_auth_config(tmp_path, monkeypatch)
        host = "ghcr.io"
        fake_http.respond(
            "GET",
            f"https://{host}/v2/",
            401,
            headers={"www-authenticate": f'Bearer realm="https://{host}/token",service="{host}"'},
        )
        fake_http.respond(
            "GET",
            f"https://{host}/token?service={host}&scope=repository:huggingface/r2e-bootstrap-probe:pull,push",
            401,
        )
        result = probe(_make_auth(), "huggingface")
        assert result.reachable is True
        assert result.authenticated is False
        assert result.can_read is False
        assert result.can_write is False
        # Helper hint should be GHCR-specific
        assert result.helper_hint and "ghcr" in result.helper_hint.lower()


class TestL3ReadDenied:
    def test_token_works_but_no_read(
        self, fake_http: _FakeHTTP, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _inline_auth_config(tmp_path, monkeypatch)
        host = "ghcr.io"
        ns = "someorg"
        fake_http.respond(
            "GET",
            f"https://{host}/v2/",
            401,
            headers={"www-authenticate": f'Bearer realm="https://{host}/token",service="{host}"'},
        )
        fake_http.respond(
            "GET",
            f"https://{host}/token?service={host}&scope=repository:{ns}/r2e-bootstrap-probe:pull,push",
            200,
            body=json.dumps(
                {"token": "abc", "scope": f"repository:{ns}/r2e-bootstrap-probe:pull,push"}
            ).encode(),
        )
        fake_http.respond(
            "HEAD",
            f"https://{host}/v2/{ns}/r2e-bootstrap-probe/manifests/latest",
            403,
        )
        result = probe(_make_auth(), ns)
        assert result.authenticated is True
        assert result.can_read is False


class TestL4WriteScope:
    def test_pull_only_token_skips_write_round_trip(
        self, fake_http: _FakeHTTP, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the token server only grants `pull`, we shouldn't even try L4."""
        _inline_auth_config(tmp_path, monkeypatch)
        host = "ghcr.io"
        ns = "huggingface"
        fake_http.respond(
            "GET",
            f"https://{host}/v2/",
            401,
            headers={"www-authenticate": f'Bearer realm="https://{host}/token",service="{host}"'},
        )
        # Token granted only `pull`
        fake_http.respond(
            "GET",
            f"https://{host}/token?service={host}&scope=repository:{ns}/r2e-bootstrap-probe:pull,push",
            200,
            body=json.dumps(
                {"token": "abc", "scope": f"repository:{ns}/r2e-bootstrap-probe:pull"}
            ).encode(),
        )
        fake_http.respond(
            "HEAD",
            f"https://{host}/v2/{ns}/r2e-bootstrap-probe/manifests/latest",
            404,
        )
        result = probe(_make_auth(), ns)
        assert result.authenticated is True
        assert result.can_read is True
        assert result.can_write is False
        assert "push" in (result.error or "")
        # No POST call should have been issued
        methods = [c[0] for c in fake_http.calls]
        assert "POST" not in methods

    def test_ecr_repo_not_exist(
        self, fake_http: _FakeHTTP, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ECR returns 404 on POST when the repo doesn't pre-exist."""
        _inline_auth_config(tmp_path, monkeypatch, host="123.dkr.ecr.us-east-1.amazonaws.com")
        host = "123.dkr.ecr.us-east-1.amazonaws.com"
        ns = "r2e"
        fake_http.respond(
            "GET",
            f"https://{host}/v2/",
            401,
            headers={"www-authenticate": f'Bearer realm="https://{host}/token",service="{host}"'},
        )
        fake_http.respond(
            "GET",
            f"https://{host}/token?service={host}&scope=repository:{ns}/r2e-bootstrap-probe:pull,push",
            200,
            body=json.dumps(
                {"token": "x", "scope": f"repository:{ns}/r2e-bootstrap-probe:pull,push"}
            ).encode(),
        )
        fake_http.respond(
            "HEAD",
            f"https://{host}/v2/{ns}/r2e-bootstrap-probe/manifests/latest",
            404,
        )
        fake_http.respond(
            "POST",
            f"https://{host}/v2/{ns}/r2e-bootstrap-probe/blobs/uploads/",
            404,
        )
        auth = RegistryAuth(host, RegistryKind.ECR_PRIVATE, CredentialSource.INLINE)
        result = probe(auth, ns)
        assert result.authenticated is True
        assert result.can_read is True
        assert result.can_write is False
        assert (
            "pre-create" in (result.error or "").lower() or "exist" in (result.error or "").lower()
        )


class TestSelectBest:
    def _make_result(self, kind: RegistryKind, *, pushable: bool = True) -> ProbeResult:
        return ProbeResult(
            host=f"{kind.value}.example",
            kind=kind,
            namespace="ns",
            levels_checked=(1, 2, 3, 4),
            reachable=True,
            authenticated=True,
            can_read=pushable,
            can_write=pushable,
        )

    def test_prefers_ghcr_over_docker_hub(self) -> None:
        ghcr = self._make_result(RegistryKind.GHCR)
        hub = self._make_result(RegistryKind.DOCKER_HUB)
        best = select_best([hub, ghcr])
        assert best is ghcr

    def test_filters_non_pushable(self) -> None:
        ghcr = self._make_result(RegistryKind.GHCR, pushable=False)
        gar = self._make_result(RegistryKind.GCP_AR)
        best = select_best([ghcr, gar])
        assert best is gar

    def test_empty_returns_none(self) -> None:
        assert select_best([]) is None

    def test_all_non_pushable_returns_none(self) -> None:
        results = [
            self._make_result(RegistryKind.GHCR, pushable=False),
            self._make_result(RegistryKind.DOCKER_HUB, pushable=False),
        ]
        assert select_best(results) is None
