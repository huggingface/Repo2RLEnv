"""Snapshot remote task definitions and receipts, never container images."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from pathlib import Path

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
            if p.stat().st_size > 25 * 1024 * 1024:
                skipped.append(str(p))
                continue
            name = str(Path(label) / p.relative_to(root))
            archive.add(p, arcname=name, recursive=False)
            index[name] = hashlib.sha256(p.read_bytes()).hexdigest()
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
