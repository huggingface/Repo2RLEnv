"""Solve-time dependency controls for the emitted egress overlay.

Opt-in with ``R2E_DOCKER_TESTS=1``, like the rest of the container suite.

The grading controls (oracle, no-op, unrelated-target regression) live next to
the fixtures they reuse, in ``tests/test_separate_verifier_container.py``, as
``test_go_grading_controls_under_the_egress_overlay`` and its Rust twin. This
module holds the checks those fixtures cannot express: the merged Go and Rust
fixtures are stdlib-only by design, so a dependency the image does not hold has
to come from the pinned fixtures under ``tests/fixtures/egress_guard/``.

* a deliberate cache miss fails fast and says why, instead of hanging;
* a vendored Go module still builds with the registry blocked;
* the agent can still reach its allowed model endpoint.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from repo2rlenv.bootstrap.spec import LanguageHint
from tests.test_separate_verifier_container import _overlay_docker_args

pytestmark = pytest.mark.skipif(
    os.environ.get("R2E_DOCKER_TESTS") != "1" or shutil.which("docker") is None,
    reason="set R2E_DOCKER_TESTS=1 to build and run the overlay fixtures",
)

FIXTURES = Path(__file__).parent / "fixtures" / "egress_guard"
GO_IMAGE = "r2e-egress-overlay-go:fixture"
RUST_IMAGE = "r2e-egress-overlay-rust:fixture"

GO_DOCKERFILE = (
    "FROM golang:1.23-bookworm\n"
    "COPY repo /workspace\n"
    "WORKDIR /workspace\n"
    # Dependencies installed at build time, as bootstrap's rebuild_cmds does, and
    # vendored so the vendored-build control has a tree to build from.
    "RUN go mod download && go mod vendor\n"
)
RUST_DOCKERFILE = (
    "FROM rust:1.83-slim-bookworm\n"
    "COPY repo /workspace\n"
    "WORKDIR /workspace\n"
    # Fills CARGO_HOME with the pinned crate; the solve phase must not need it.
    "RUN cargo fetch\n"
)


def _build(image: str, root: Path, language: str, dockerfile: str, timeout: int) -> str:
    """Build the pinned fixture image with its dependencies installed."""
    shutil.copytree(FIXTURES / language, root / "repo")
    (root / "Dockerfile").write_text(dockerfile)
    subprocess.run(["docker", "build", "-q", "-t", image, str(root)], check=True, timeout=timeout)
    return image


@pytest.fixture(scope="module")
def go_overlay_image(tmp_path_factory) -> str:
    root = tmp_path_factory.mktemp("egress-overlay-go")
    return _build(GO_IMAGE, root, "go", GO_DOCKERFILE, 900)


@pytest.fixture(scope="module")
def rust_overlay_image(tmp_path_factory) -> str:
    root = tmp_path_factory.mktemp("egress-overlay-rust")
    return _build(RUST_IMAGE, root, "rust", RUST_DOCKERFILE, 900)


def _run(image: str, args: tuple[str, ...], script: str, timeout: int = 300):
    return subprocess.run(
        ["docker", "run", "--rm", *args, image, "bash", "-lc", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_go_vendored_build_succeeds_offline(go_overlay_image: str):
    """Vendoring is preserved: no GOFLAGS, vendor/ present, build works offline."""
    done = _run(
        go_overlay_image,
        _overlay_docker_args(LanguageHint.GO),
        "cd /workspace && "
        'test -z "$(go env GOFLAGS)" && '
        "test -f vendor/modules.txt && "
        "go build ./... && "
        "go test ./... && "
        "echo OFFLINE_OK",
    )
    assert done.returncode == 0, done.stderr
    assert "OFFLINE_OK" in done.stdout, done.stdout


def test_go_cache_miss_fails_promptly(go_overlay_image: str):
    """A module the image does not hold must fail fast, and say why."""
    started = time.monotonic()
    done = _run(
        go_overlay_image,
        _overlay_docker_args(LanguageHint.GO),
        "cd /workspace && go mod download github.com/google/uuid@v1.5.0",
        timeout=120,
    )
    elapsed = time.monotonic() - started
    assert done.returncode != 0
    assert "GOPROXY=off" in (done.stderr + done.stdout), done.stderr
    assert elapsed < 60, f"cache miss took {elapsed:.1f}s; it should fail immediately"


def test_rust_cache_miss_fails_promptly(rust_overlay_image: str):
    """Same for cargo: a crate outside the image fails with the offline message."""
    started = time.monotonic()
    done = _run(
        rust_overlay_image,
        _overlay_docker_args(LanguageHint.RUST),
        "cd /workspace && "
        "printf '\\n[dependencies.serde]\\nversion = \"=1.0.200\"\\n' >> Cargo.toml && "
        "cargo build",
        timeout=180,
    )
    elapsed = time.monotonic() - started
    assert done.returncode != 0
    combined = (done.stderr + done.stdout).lower()
    assert "offline" in combined or "network" in combined, combined[-2000:]
    assert elapsed < 120, f"cache miss took {elapsed:.1f}s; it should fail immediately"


def test_agent_reaches_its_allowed_model_endpoint(go_overlay_image: str):
    """The overlay must not blackhole the model transport the agent needs."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            seen.append(self.rfile.read(length).decode("utf-8", "replace"))
            body = b'{"id":"msg_1","type":"message","role":"assistant","content":[]}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        done = _run(
            go_overlay_image,
            (
                *_overlay_docker_args(LanguageHint.GO),
                "--add-host=host.docker.internal:host-gateway",
            ),
            "python3 - <<'PY'\n"
            "import json, urllib.request\n"
            f"req = urllib.request.Request('http://host.docker.internal:{port}/v1/messages',\n"
            "    data=json.dumps({'model': 'mock', 'messages': []}).encode(),\n"
            "    headers={'content-type': 'application/json'})\n"
            "print(urllib.request.urlopen(req, timeout=10).status)\n"
            "PY",
            timeout=120,
        )
    finally:
        server.shutdown()
    assert done.returncode == 0, done.stderr
    assert "200" in done.stdout, done.stdout
    assert seen, "the mock endpoint never received a request"
