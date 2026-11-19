from __future__ import annotations

from repo2rlenv.bootstrap import docker as d

_429 = (
    "Error response from daemon: failed to resolve reference "
    '"docker.io/library/rust:1-slim": unexpected status from HEAD request: '
    "429 Too Many Requests"
)


def _res(code: int, err: str = "") -> d.ExecResult:
    return d.ExecResult(exit_code=code, stdout="", stderr=err, duration_sec=0.0)


def test_is_rate_limited():
    assert d.is_rate_limited(_429)
    assert not d.is_rate_limited("manifest unknown")


def test_retries_on_429_then_succeeds(monkeypatch):
    calls = iter([_res(1, _429), _res(1, _429), _res(0)])
    monkeypatch.setattr(d, "pull_image_streaming", lambda *a, **k: next(calls))
    slept: list[float] = []
    out = d.pull_with_rate_limit_retry("rust:1-slim", sleep=slept.append)
    assert out.ok
    assert len(slept) == 2


def test_no_retry_on_other_errors(monkeypatch):
    n = {"c": 0}

    def fake(*a, **k):
        n["c"] += 1
        return _res(1, "manifest unknown")

    monkeypatch.setattr(d, "pull_image_streaming", fake)
    out = d.pull_with_rate_limit_retry("x:y", sleep=lambda s: None)
    assert not out.ok and n["c"] == 1


def test_gives_up_after_delays(monkeypatch):
    monkeypatch.setattr(d, "pull_image_streaming", lambda *a, **k: _res(1, _429))
    out = d.pull_with_rate_limit_retry("x:y", delays=(1.0, 2.0), sleep=lambda s: None)
    assert not out.ok
