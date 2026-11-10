"""Gate: skip trajectories whose final answers can't be trusted as gold.

Uses atif-scan (https://github.com/huggingface/atif-scan) when it is installed; otherwise the
gate lets everything through and says so in the report.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

# atif-scan findings meaning the agent saw benchmark material or tampered with grading.
BLOCKING_FINDINGS = {
    "access.benchmark_material_received",
    "lookup.benchmark_content_obtained",
    "lookup.own_task_files",
    "tamper.reward_write",
    "tamper.test_files",
    "harness.verifier_feedback_injected",
}


def scan_trajectory(path: Path) -> tuple[bool, str]:
    """Return (passed, explanation)."""
    atif_scan = shutil.which("atif-scan")
    if atif_scan is None:
        return True, "skipped (atif-scan not installed)"
    try:
        process = subprocess.run(
            [atif_scan, str(path), "--format", "json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"scan error: {type(error).__name__}"
    try:
        assessments = json.loads(process.stdout)["inputs"][0]["assessments"]
    except (ValueError, KeyError, IndexError, TypeError):
        return False, "scan error: output unreadable"
    if not isinstance(assessments, list) or any(not isinstance(a, dict) for a in assessments):
        return False, "scan error: invalid assessments"
    statuses = {a.get("id"): a.get("status") for a in assessments}
    unresolved = sorted(
        key
        for key in BLOCKING_FINDINGS
        if statuses.get(key) not in {"match", "no_match", "not_applicable"}
    )
    if unresolved:
        return False, "scan incomplete: " + ", ".join(unresolved)
    findings = sorted(
        a["id"] for a in assessments if a["status"] == "match" and a["id"] in BLOCKING_FINDINGS
    )
    return (False, "blocked: " + ", ".join(findings)) if findings else (True, "clean")
