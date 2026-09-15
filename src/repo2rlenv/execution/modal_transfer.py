"""Verified, bounded directory upload on an already allocated Modal sandbox."""

from __future__ import annotations

import hashlib
import shlex
import tempfile
from pathlib import Path
from uuid import uuid4

from harbor.environments.tar_transfer import pack_dir_to_file, remote_unpack_command

from repo2rlenv.execution.lifecycle import save_record


async def upload_directory(environment, source_dir: Path | str, target_dir: str) -> None:
    """Retry only the same temporary archive transfer, before extracting any bytes."""
    if not environment._sandbox:
        raise RuntimeError("Sandbox is not running")
    source = Path(source_dir)
    if not source.is_dir():
        raise FileNotFoundError(source)
    identity = uuid4().hex
    remote = f"/tmp/tasksmith_upload_{identity}.tar.gz"
    receipt = environment.accounting.directory / "transfers" / f"{identity}.json"
    record = {"target": target_dir, "archive": remote, "attempts": [], "state": "preparing"}
    shell = environment._default_shell
    try:
        with tempfile.TemporaryDirectory(prefix="tasksmith-upload-") as temporary:
            archive = Path(temporary) / "upload.tar.gz"
            pack_dir_to_file(source, archive)
            with archive.open("rb") as handle:
                expected = hashlib.file_digest(handle, "sha256").hexdigest()
            record.update(sha256=expected, bytes=archive.stat().st_size, state="uploading")
            save_record(receipt, record)
            for attempt in range(3):
                check = {"attempt": attempt + 1}
                record["attempts"].append(check)
                save_record(receipt, record)
                try:
                    await environment._sdk_upload_file(archive, remote)
                    result = await environment._sdk_exec(
                        "sha256sum -- " + shlex.quote(remote), shell=shell, timeout_sec=60
                    )
                    observed = result.stdout.split()[0] if result.stdout.split() else ""
                    check["observed_sha256"] = observed
                    if result.return_code or observed != expected:
                        raise RuntimeError("Uploaded archive checksum does not match local bytes")
                    check["verified"] = True
                    save_record(receipt, record)
                    break
                except Exception as exc:
                    check["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
                    save_record(receipt, record)
                    if attempt == 2:
                        raise
            record["state"] = "extracting"
            save_record(receipt, record)
            result = await environment._sdk_exec(
                remote_unpack_command(remote, target_dir), shell=shell, timeout_sec=600
            )
            if result.return_code:
                raise RuntimeError(f"Verified archive extraction failed: {result.stderr[-2000:]}")
            record["state"] = "completed"
    except BaseException:
        record["state"] = "failed"
        raise
    finally:
        try:
            await environment._sdk_exec(
                "rm -f -- " + shlex.quote(remote), shell=shell, timeout_sec=10
            )
        except Exception as exc:
            record["cleanup_error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
        save_record(receipt, record)
