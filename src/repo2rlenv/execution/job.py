"""Remote process supervisor with a durable outcome, independent of its caller."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import uuid
from pathlib import Path

from repo2rlenv.execution.lifecycle import now, save_record

JOB_LABEL = "repo2rlenv.job"


def container_labels() -> list[str]:
    """Attach the supervisor's ownership label to a recipe's Docker resources."""
    job_id = os.environ.get("REPO2RLENV_JOB_ID")
    return ["--label", f"{JOB_LABEL}={job_id}"] if job_id else []


def _cleanup_containers(job_id: str) -> dict:
    # Killing a Docker CLI does not stop the container it started. Select only
    # this supervisor's label; unrelated jobs and cached images remain intact.
    listed = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label={JOB_LABEL}={job_id}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if listed.returncode:
        return {"passed": False, "reason": "container_lookup_failed"}
    containers = listed.stdout.split()
    if not containers:
        return {"passed": True, "removed": 0}
    removed = subprocess.run(
        ["docker", "rm", "-f", *containers],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return {"passed": removed.returncode == 0, "removed": len(containers)}


def execute(directory: Path, command: list[str], *, timeout_sec: int) -> int:
    if os.environ.get("REPO2RLENV_REMOTE_WORKER") != "1":
        raise RuntimeError("Job supervision runs inside a remote worker only")
    if not command or not 1 <= timeout_sec <= 14400:
        raise ValueError("A job needs a command and a bounded deadline")
    directory.mkdir(parents=True, exist_ok=True)
    receipt = directory / "job.json"
    with (directory / ".job-claim").open("x"):
        pass
    record = {
        "job_id": uuid.uuid4().hex,
        "state": "starting",
        "started_at": now(),
        "supervisor_pid": os.getpid(),
        "command": command,
    }
    save_record(receipt, record)

    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    previous = signal.signal(signal.SIGTERM, terminate)
    process = None
    try:
        with (
            (directory / "stdout.txt").open("w") as stdout,
            (directory / "stderr.txt").open("w") as stderr,
        ):
            process = subprocess.Popen(
                command,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                env={**os.environ, "REPO2RLENV_JOB_ID": record["job_id"]},
            )
            record.update(state="running", pid=process.pid)
            save_record(receipt, record)
            code = process.wait(timeout=timeout_sec)
            record.update(state="completed", returncode=code)
    except BaseException as exc:
        record.update(state="failed", exception_type=type(exc).__name__)
        raise
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        try:
            record["cleanup"] = _cleanup_containers(record["job_id"])
        except (OSError, subprocess.TimeoutExpired) as exc:
            record["cleanup"] = {"passed": False, "exception_type": type(exc).__name__}
        if not record["cleanup"]["passed"]:
            record.update(state="failed", cleanup_pending=True)
        record["finished_at"] = now()
        save_record(receipt, record)
        signal.signal(signal.SIGTERM, previous)
    return code if record["cleanup"]["passed"] else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--timeout-sec", required=True, type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    raise SystemExit(execute(args.directory, command, timeout_sec=args.timeout_sec))


if __name__ == "__main__":
    main()
