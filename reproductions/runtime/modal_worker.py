"""Remote Docker worker; no local Docker execution or task generation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import modal
from dotenv import dotenv_values

from budget import now, reserve

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "reproductions" / "runs"
APP = "repo2rlenv-upstream-reproductions"


def worker_image() -> modal.Image:
    return (
        modal.Image.from_registry("ubuntu:24.04", add_python="3.12")
        .env({"DEBIAN_FRONTEND": "noninteractive"})
        .apt_install("docker.io", "docker-buildx", "docker-compose-v2", "git", "curl",
                     "ca-certificates", "nodejs", "npm", "python3-venv", "unzip",
                     "jq", "build-essential", "squashfs-tools", "fuse3", "uidmap")
        .pip_install("uv==0.10.9")
        .run_commands("mkdir -p /work /evidence", "chmod 1777 /work /evidence")
    )


def save(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n")
    temporary.replace(path)


def create(name: str, hours: float, cloud_reserve: str) -> None:
    if not 0 < hours <= 4:
        raise ValueError("Worker lifetime must be between 0 and 4 hours")
    receipt = RUNS / f"{name}.json"
    if receipt.exists():
        raise ValueError("Worker receipt exists; inspect/stop it before using a new name")
    reserve(f"cloud:{name}", cloud_reserve, f"Modal image build + {hours}h worker allowance")
    record = {"provider": "modal", "name": name, "app": APP, "state": "creating",
              "created_at": now(), "timeout_seconds": int(hours * 3600),
              "cpu_limit": 4, "memory_mib": 16384, "cloud_reservation": cloud_reserve}
    save(receipt, record)
    try:
        with modal.enable_output():
            sandbox = modal.Sandbox.create(
                "/usr/bin/dockerd", "--storage-driver=overlay2",
                app=modal.App.lookup(APP, create_if_missing=True), name=name,
                image=worker_image(), cpu=(4, 4), memory=16384,
                timeout=int(hours * 3600), experimental_options={"vm_runtime": True},
            )
        record.update(sandbox_id=sandbox.object_id, state="running", started_at=now())
        save(receipt, record)
        print(json.dumps(record, indent=2))
    except BaseException:
        record["state"] = "creation_uncertain"
        save(receipt, record)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "exec", "upload", "download", "stop", "status"])
    parser.add_argument("--name", required=True)
    parser.add_argument("--hours", type=float, default=2)
    parser.add_argument("--cloud-reserve", default="10")
    parser.add_argument("--script", type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--credential", action="append", default=[])
    parser.add_argument("--local", type=Path)
    parser.add_argument("--remote")
    args = parser.parse_args()
    if args.action == "create":
        create(args.name, args.hours, args.cloud_reserve)
        return
    receipt = RUNS / f"{args.name}.json"
    record = json.loads(receipt.read_text())
    if "sandbox_id" not in record:
        raise ValueError("Creation uncertain; resolve named sandbox via provider before retrying")
    sandbox = modal.Sandbox.from_id(record["sandbox_id"])
    if args.action == "exec":
        if not args.script:
            parser.error("exec requires --script")
        selected = {}
        if args.credential:
            credentials = dotenv_values(ROOT / ".env")
            for key in args.credential:
                if key not in {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "HF_TOKEN"}:
                    raise ValueError("Unsupported credential")
                if not credentials.get(key):
                    raise ValueError(f"Missing credential: {key}")
                selected[key] = credentials[key]
        process = sandbox.exec("bash", "-se", "-o", "pipefail", "-c", args.script.read_text(),
                               timeout=args.timeout, stderr=modal.stream_type.StreamType.STDOUT,
                               secrets=[modal.Secret.from_dict(selected)] if selected else [])
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
        process.wait()
        raise SystemExit(process.returncode)
    elif args.action == "upload":
        sandbox.filesystem.copy_from_local(str(args.local), args.remote)
    elif args.action == "download":
        args.local.parent.mkdir(parents=True, exist_ok=True)
        sandbox.filesystem.copy_to_local(args.remote, str(args.local))
    elif args.action == "stop":
        sandbox.terminate(wait=True)
        record.update(state="terminated", stopped_at=now())
        save(receipt, record)
        print(json.dumps(record, indent=2))
    else:
        print(json.dumps({**record, "returncode": sandbox.poll()}, indent=2))


if __name__ == "__main__":
    main()
