"""Project original validation reports into local instances without publishing branches."""

from __future__ import annotations

import json
from pathlib import Path

from swesmith.profiles import registry


def main() -> None:
    profile = registry.get("mewwts__addict.75284f95")
    patches = json.loads(Path(f"logs/bug_gen/{profile.repo_name}_all_patches_n10.json").read_text())
    accepted, receipts = [], []
    for instance in patches:
        report_path = Path("logs/run_validation") / profile.repo_name / instance["instance_id"] / "report.json"
        if not report_path.exists():
            receipts.append({"instance_id": instance["instance_id"], "status": "missing_report"})
            continue
        report = json.loads(report_path.read_text())
        # At the pinned revision valid.py compares buggy pre-gold against healthy post-gold.
        # Its FAIL_TO_PASS is already in evaluation orientation; do not flip it again.
        passes = bool(report.get("FAIL_TO_PASS")) and not report.get("timed_out")
        receipts.append({"instance_id": instance["instance_id"], "native_accepted": passes,
                         "report": str(report_path), "counts": {
                             key: len(value) for key, value in report.items() if isinstance(value, list)}})
        if passes:
            accepted.append({**instance, "FAIL_TO_PASS": report["FAIL_TO_PASS"],
                             "PASS_TO_PASS": report.get("PASS_TO_PASS", []),
                             "repo": profile.mirror_name, "image_name": profile.image_name,
                             "base_commit": profile.commit})
    destination = Path("/work/swesmith")
    (destination / "validation-summary.json").write_text(json.dumps(receipts, indent=2))
    (destination / "validated.json").write_text(json.dumps(accepted[:5], indent=2))
    print(json.dumps({"attempted": len(patches), "native_accepted": len(accepted),
                      "selected_for_issue_generation": min(5, len(accepted))}, indent=2))


if __name__ == "__main__":
    main()
