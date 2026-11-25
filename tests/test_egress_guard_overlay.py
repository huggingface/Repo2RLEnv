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

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
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
    "FROM node:24-bookworm-slim AS agent\n"
    "RUN npm install --prefix /opt/agent --no-audit --no-fund "
    "@anthropic-ai/claude-code@2.1.332 && /opt/agent/node_modules/.bin/claude --version\n"
    "FROM golang:1.23-bookworm\n"
    "COPY --from=agent /opt/agent /opt/agent\n"
    "COPY --from=agent /usr/local/bin/node /usr/local/bin/node\n"
    'ENV PATH="/opt/agent/node_modules/.bin:${PATH}"\n'
    "COPY repo /workspace\n"
    "WORKDIR /workspace\n"
    # Dependencies installed at build time, as bootstrap's rebuild_cmds does, and
    # vendored so the vendored-build control has a tree to build from.
    "RUN go mod tidy && go mod vendor\n"
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
    (root / "Dockerfile").write_text(dockerfile, encoding="utf-8")
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
    name = f"r2e-egress-{uuid.uuid4().hex}"
    try:
        return subprocess.run(
            ["docker", "run", "--rm", "--name", name, *args, image, "bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    finally:
        # A host-side timeout kills the client, not the container it started.
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
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


def test_rust_cached_build_succeeds_offline(rust_overlay_image: str):
    done = _run(
        rust_overlay_image,
        _overlay_docker_args(LanguageHint.RUST),
        "cargo test --locked && echo OFFLINE_OK",
    )
    assert done.returncode == 0, done.stderr
    assert "OFFLINE_OK" in done.stdout, done.stdout


def test_agent_reaches_its_allowed_model_endpoint(go_overlay_image: str):
    """Start the real preinstalled agent against a fake Anthropic endpoint."""
    seen: list[dict] = []
    answer = "OVERLAY_MODEL_OK"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            request = json.loads(self.rfile.read(length))
            path = self.path.split("?", 1)[0]
            if path == "/v1/messages/count_tokens":
                body = b'{"input_tokens": 10}'
                content_type = "application/json"
            elif path == "/v1/messages":
                seen.append(request)
                message = {
                    "id": "msg_overlay",
                    "type": "message",
                    "role": "assistant",
                    "model": request["model"],
                    "content": [{"type": "text", "text": answer}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }
                content_type = "application/json"
                body = json.dumps(message).encode()
                if request.get("stream"):
                    content_type = "text/event-stream"
                    events = [
                        (
                            "message_start",
                            {"message": {**message, "content": [], "stop_reason": None}},
                        ),
                        (
                            "content_block_start",
                            {"index": 0, "content_block": {"type": "text", "text": ""}},
                        ),
                        (
                            "content_block_delta",
                            {"index": 0, "delta": {"type": "text_delta", "text": answer}},
                        ),
                        ("content_block_stop", {"index": 0}),
                        (
                            "message_delta",
                            {
                                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                "usage": {"output_tokens": 5},
                            },
                        ),
                        ("message_stop", {}),
                    ]
                    body = "".join(
                        f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n"
                        for kind, data in events
                    ).encode()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    # Linux containers reach the host's bridge interface, not its loopback.
    # This opt-in fixture receives only a fixed fake credential.
    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        done = _run(
            go_overlay_image,
            (
                *_overlay_docker_args(LanguageHint.GO),
                "--add-host=host.docker.internal:host-gateway",
                f"--env=ANTHROPIC_BASE_URL=http://host.docker.internal:{port}",
                "--env=ANTHROPIC_API_KEY=fixture-not-a-real-key",
                "--env=CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
                "--env=DISABLE_AUTOUPDATER=1",
                "--env=NO_PROXY=*",
            ),
            "claude --version && "
            "claude --print --model claude-sonnet-4-6 --output-format json "
            "--max-turns 1 --tools '' --no-session-persistence --setting-sources '' "
            f"'Reply with {answer}'",
            timeout=120,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert done.returncode == 0, done.stderr
    assert "2.1.332" in done.stdout, done.stdout
    assert answer in done.stdout, done.stdout
    assert any(answer in json.dumps(request.get("messages")) for request in seen), seen
