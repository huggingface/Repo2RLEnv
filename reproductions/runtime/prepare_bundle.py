"""Package a downloaded remote snapshot for the owner's artifact repository.

Only Harbor contract files enter task folders. Raw author/solver evidence stays
in a separate archive; execution success is never promoted to quality approval.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from harbor_artifacts import export_native, hashes

ROOTS = {
    "seta-seed2synth/harbor": "tasks/seta-seed2synth",
    "seta-evol/harbor": "tasks/seta-evol",
    "endless-terminals/harbor": "tasks/endless-terminals",
    "endless-terminals/harbor-batch-02": "tasks/endless-terminals",
    "tmax/harbor": "tasks/tmax",
    "swe-smith/harbor": "tasks/swe-smith",
    "seta-seed2synth/offline-v2": "variants/seta-offline",
    "seta-seed2synth/reference-fixed": "variants/seta-reference-precision",
    "tmax/harbor-runtime-fixed": "variants/tmax-verifier-dependency",
    "swe-smith/blind-audit": "variants/swesmith-blind-runtime",
    "cli-gym/harbor-v2": "tasks/cli-gym",
    "swe-flow/export-v3-harbor": "tasks/swe-flow",
    "swe-gen/harbor": "tasks/swe-gen",
    "dataarc-terminal/harbor": "tasks/dataarc-terminal",
    "r2e/harbor": "tasks/r2e",
    "scaler/harbor": "tasks/scaler",
    "terminalworld/harbor": "tasks/terminalworld",
    "swe-rebench-v2/harbor": "replayed-tasks/swe-rebench-v2",
}


def audit_records(value, pointer=""):
    """Locate execution receipts within native/export comparison envelopes."""
    if isinstance(value, dict):
        if "task_hashes" in value and "execution_contrast_passed" in value:
            yield pointer, value
        else:
            for key, child in value.items():
                yield from audit_records(child, f"{pointer}/{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from audit_records(child, f"{pointer}/{index}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--review", type=Path, required=True)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=False)
    review = json.loads(args.review.read_text())
    audits = []
    for path in sorted(args.snapshot.rglob("*.json")):
        if "audit" not in path.name:
            continue
        for pointer, report in audit_records(json.loads(path.read_text())):
            audits.append((str(path.relative_to(args.snapshot)) + "#" + pointer, report))
    records = []
    for origin, output in ROOTS.items():
        for source in sorted((args.snapshot / origin).glob("*/task.toml")):
            task = source.parent
            destination = args.destination / output / task.name
            exported = export_native(task, destination)
            matches = []
            for path, report in audits:
                if report["task_hashes"] == exported["files"]:
                    matches.append(
                        {
                            "receipt": path,
                            "execution_contrast_passed": report["execution_contrast_passed"],
                        }
                    )
            records.append(
                {
                    "id": task.name,
                    "path": str(destination.relative_to(args.destination)),
                    "snapshot_source": str(task.relative_to(args.snapshot)),
                    "files": exported["files"],
                    "oracle_present": exported["oracle_present"],
                    "matching_execution_receipts": matches,
                    "training_approved": False,
                    "review": review.get(task.name, {"status": "not_reviewed"}),
                }
            )
    if not records:
        raise ValueError("No Harbor tasks found in the snapshot")
    (args.destination / "task-index.json").write_text(json.dumps(records, indent=2) + "\n")
    shutil.copy2(args.review, args.destination / "quality-review.json")
    # Validate that the snapshot's complete content can be hashed without host links.
    manifest = {"snapshot_files": hashes(args.snapshot), "task_entries": len(records)}
    (args.destination / "snapshot-files.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"task_entries_including_variants": len(records)}))


if __name__ == "__main__":
    main()
