"""Remote provider acceptance probes, using owned temporary Docker resources."""

from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from pathlib import Path

from repo2rlenv.execution.base import RemoteWorker
from repo2rlenv.execution.lifecycle import now, prepare_docker, save_record


def probe_worker(worker: RemoteWorker, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    token = uuid.uuid4().hex
    directory = f"/work/probe-{token}"
    image = f"repo2rlenv-probe:{token}"
    container = f"r2e-probe-{token}"
    record = {"worker_id": worker.id, "started_at": now(), "checks": {}}

    def command(label, argv, timeout=120):
        result = worker.exec(argv, timeout=timeout)
        (output / f"{label}.stdout").write_text(result.stdout)
        (output / f"{label}.stderr").write_text(result.stderr)
        result.checked(label)
        record["checks"][label] = True
        save_record(output / "probe.json", record)
        return result

    try:
        prepare_docker(worker)
        command("mkdir", ["mkdir", "-p", directory])
        with tempfile.TemporaryDirectory() as local_dir:
            local = Path(local_dir)
            marker = b"repo2rlenv binary transfer\x00\xff\n"
            (local / "marker").write_bytes(marker)
            (local / "Dockerfile").write_text("FROM python:3.12-slim\nCOPY marker /marker\n")
            worker.upload(local / "marker", directory + "/marker")
            worker.upload(local / "Dockerfile", directory + "/Dockerfile")
            worker.download(directory + "/marker", local / "returned")
            if (local / "returned").read_bytes() != marker:
                raise ValueError("Remote file transfer changed bytes")
            record["checks"]["binary_transfer"] = True
            record["marker_sha256"] = hashlib.sha256(marker).hexdigest()
        command("build", ["docker", "build", "-t", image, directory], timeout=600)
        program = """import pathlib, socket
assert pathlib.Path('/marker').read_bytes() == b'repo2rlenv binary transfer\\x00\\xff\\n'
try:
    socket.create_connection(('1.1.1.1', 443), timeout=2)
except OSError:
    pass
else:
    raise AssertionError('Unexpected outbound access')
pathlib.Path('/changed').write_text('ephemeral')
"""
        command(
            "isolated_run",
            [
                "docker",
                "run",
                "--rm",
                "--name",
                container,
                "--network",
                "none",
                image,
                "python",
                "-c",
                program,
            ],
        )
        command(
            "fresh_reset",
            [
                "docker",
                "run",
                "--rm",
                "--name",
                container,
                "--network",
                "none",
                image,
                "python",
                "-c",
                "from pathlib import Path; assert not Path('/changed').exists()",
            ],
        )
    finally:
        cleanup = worker.exec(["docker", "rm", "-f", container], timeout=30)
        # A successful --rm already removed it; image removal is an independent
        # positive check that no probe container still holds that image.
        removed = worker.exec(["docker", "image", "rm", image], timeout=60)
        record["cleanup"] = {
            "container_returncode": cleanup.returncode,
            "image_returncode": removed.returncode,
        }
        record["passed"] = (
            all(
                record["checks"].get(check, False)
                for check in ("binary_transfer", "build", "isolated_run", "fresh_reset")
            )
            and removed.returncode == 0
        )
        record["finished_at"] = now()
        (output / "probe.json").write_text(json.dumps(record, indent=2) + "\n")
    if not record["passed"]:
        raise RuntimeError("Remote probe or cleanup failed; inspect probe.json")
    return record
