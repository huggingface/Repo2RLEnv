"""Snapshot remote task definitions and receipts, never container images."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path, PurePosixPath

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("destination", type=Path)
args = parser.parse_args()
roots = {
    "evidence": Path("/evidence"),
    "seta-seed2synth/harbor": Path("/work/seta/harbor"),
    "seta-seed2synth/offline-v2": Path("/work/seta/offline-v2"),
    "seta-seed2synth/reference-fixed": Path("/work/seta/reference-fixed"),
    "seta-evol/harbor": Path("/work/seta/evol-harbor"),
    "seta-seed2synth/seeds": Path("/work/seta/seed_data"),
    "seta-seed2synth/native": Path("/work/seta/synth_data"),
    "seta-evol/native": Path("/work/seta/evol_data"),
    "endless-terminals/harbor": Path("/work/endless/harbor"),
    "endless-terminals/native-batch-01": Path("/work/endless/native-batch-01"),
    "endless-terminals/converted-batch-01": Path("/work/endless/converted-batch-01"),
    "endless-terminals/native-batch-02": Path("/work/endless/native-batch-02"),
    "endless-terminals/converted-batch-02": Path("/work/endless/converted-batch-02"),
    "endless-terminals/harbor-batch-02": Path("/work/endless/harbor-batch-02"),
    "endless-terminals/adversarial": Path("/work/endless/adversarial"),
    "endless-terminals/adversarial-01": Path("/work/endless/adversarial-01"),
    "endless-terminals/adversarial-02": Path("/work/endless/adversarial-02"),
    "tmax/harbor": Path("/work/tmax/harbor-v2"),
    "tmax/native-smoke-01": Path("/work/tmax/native-smoke-01"),
    "tmax/harbor-runtime-fixed": Path("/work/tmax/harbor-runtime-fixed"),
    "tmax/runtime-fixed": Path("/work/tmax/runtime-fixed"),
    "swe-smith/harbor": Path("/work/swesmith/harbor"),
    "swe-smith/native-logs": Path("/work/swesmith/upstream/logs"),
    "swe-smith/blind-audit": Path("/work/swesmith/blind-audit"),
    "cli-gym/native-proposals": Path("/work/cli-gym/upstream/CLI-Gym/destruction_tasks"),
    "cli-gym/native-proposals-config": Path(
        "/work/cli-gym/upstream/CLI-Gym/destruction_tasks-config"
    ),
    "cli-gym/native-runs-config": Path("/work/cli-gym/native-runs-config"),
    "cli-gym/native-instances": Path("/work/cli-gym/upstream/CLI-Gym/problem_instances"),
    "cli-gym/harbor-v2": Path("/work/cli-gym/harbor-v2"),
    "cli-gym/audit": Path("/work/cli-gym/audit"),
    "swe-flow/native-output": Path("/work/swe-flow/native-output"),
    "swe-flow/first-adapter-audit": Path("/work/swe-flow/audit"),
    "swe-flow/export-v2-audit": Path("/work/swe-flow/exports/export-v2/audit"),
    "swe-flow/export-v3-harbor": Path("/work/swe-flow/exports/export-v3/harbor"),
    "swe-flow/export-v3-audit": Path("/work/swe-flow/exports/export-v3/audit"),
    "swe-flow/native-probes": Path("/work/swe-flow/exports/export-v3/native-probe"),
    "swe-gen/native-tasks": Path("/work/swe-gen/native-tasks"),
    "swe-gen/logs": Path("/work/swe-gen/state/logs"),
    "swe-gen/native-validation": Path("/work/swe-gen/state/harbor-jobs"),
    "swe-gen/harbor": Path("/work/swe-gen/harbor"),
    "swe-gen/audit": Path("/work/swe-gen/audit"),
    "dataarc-terminal/native-output": Path("/work/dataarc-terminal/native-output"),
    "dataarc-terminal/harbor": Path("/work/dataarc-terminal/harbor"),
    "dataarc-terminal/audit": Path("/work/dataarc-terminal/audit"),
    "harden-v0/input": Path("/work/harden-v0/input"),
    "harden-v0/native-output": Path("/work/harden-v0/native-output"),
    "r2e/native-output": Path("/work/r2e/native-output"),
    "r2e/harbor": Path("/work/r2e/harbor"),
    "r2e/audit": Path("/work/r2e/audit"),
    "scaler/native-output": Path("/work/scaler/native-output"),
    "scaler/harbor": Path("/work/scaler/harbor"),
    "scaler/audit": Path("/work/scaler/audit"),
    "terminalworld/native-tasks": Path("/work/terminalworld/native-tasks"),
    "terminalworld/native-drafts": Path("/work/terminalworld/native-drafts"),
    "terminalworld/harbor": Path("/work/terminalworld/harbor"),
    "terminalworld/audit": Path("/work/terminalworld/audit"),
    "swe-next/repo-datasets": Path("/work/swe-next/upstream/outputs/repo_datasets"),
    "swe-next/env-profiles": Path("/work/swe-next/upstream/outputs/env_profiles"),
    "swe-next/exec-summaries": Path("/work/swe-next/upstream/outputs/exec_summaries"),
    "swe-next/pr-stats": Path("/work/swe-next/upstream/outputs/pr_stats"),
    "secverifier/native-output": Path("/work/secverifier/native-output"),
    "swe-rebench-v2/native-output": Path("/work/swe-rebench-v2/native-output"),
    "swe-rebench-v2/harbor": Path("/work/swe-rebench-v2/harbor"),
    "swe-rebench-v2/audit": Path("/work/swe-rebench-v2/audit"),
}
index = {}
skipped = []
with tarfile.open(args.destination, "w:gz") as archive:
    for label, root in roots.items():
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if (
                p.is_symlink()
                or p.suffix in {".sif", ".pyc"}
                or p.name.endswith((".tar", ".tar.gz", ".tgz"))
                or p == args.destination
            ):
                skipped.append(str(p))
                continue
            if any(x in {".git", "__pycache__"} for x in p.parts):
                continue
            # TerminalWorld permits derived tasks, not redistribution of recordings.
            if label.startswith("terminalworld/") and (
                "source" in p.relative_to(root).parts
                or p.name == "recording.txt"
                or any(
                    part.startswith((".refine_workspace", ".agent_workspace"))
                    for part in p.relative_to(root).parts
                )
            ):
                continue
            if p.stat().st_size > 25 * 1024 * 1024:
                skipped.append(str(p))
                continue
            name = str(Path(label) / p.relative_to(root))
            archive.add(p, arcname=name, recursive=False)
            index[name] = hashlib.sha256(p.read_bytes()).hexdigest()
    # Retain the exact historical source uploads referenced by execution logs.
    # Unpack only regular text recipe members, never Docker/SIF archives.
    for bundle in sorted(Path("/work/recipe-bundles").glob("*.tar.gz")):
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        if bundle.name != f"{digest}.tar.gz":
            raise ValueError(f"Recipe bundle checksum mismatch: {bundle.name}")
        with tarfile.open(bundle) as source:
            for member in source.getmembers():
                relative = PurePosixPath(member.name)
                if (
                    not member.isfile()
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or member.size > 2 * 1024 * 1024
                ):
                    raise ValueError(f"Invalid recipe member: {member.name}")
                content = source.extractfile(member).read()
                name = str(PurePosixPath("recipes") / digest / relative)
                target = tarfile.TarInfo(name)
                target.size = len(content)
                target.mode = member.mode
                target.mtime = member.mtime
                archive.addfile(target, io.BytesIO(content))
                index[name] = hashlib.sha256(content).hexdigest()
    for name in ["validated.json", "validated__ig_llm.json", "validation-summary.json"]:
        p = Path("/work/swesmith") / name
        if p.exists():
            archive.add(p, arcname="swe-smith/" + name, recursive=False)
            index["swe-smith/" + name] = hashlib.sha256(p.read_bytes()).hexdigest()
manifest = {
    "archive": str(args.destination),
    "files": index,
    "skipped": skipped,
    "sha256": hashlib.sha256(args.destination.read_bytes()).hexdigest(),
}
args.destination.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2))
print(
    json.dumps(
        {
            "archive": str(args.destination),
            "files": len(index),
            "bytes": args.destination.stat().st_size,
            "skipped": len(skipped),
            "sha256": manifest["sha256"],
        }
    )
)
