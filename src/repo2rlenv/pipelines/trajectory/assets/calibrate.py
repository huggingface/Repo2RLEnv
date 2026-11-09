"""Runs inside a task's image (network off) to score candidate hidden tests.

/calibration/variants/<name>/  code variants: reference, reference_again, stub, empty, mutant_00, ...
/calibration/test_hidden.py    candidate tests
/calibration/known_files.json  optional: the project's own files (relative paths). If present, each
                               variant is laid over the image's workspace (installed dependencies
                               kept) after removing those files, instead of replacing the workspace.
Writes /calibration/results.json: {variant: {test name: "passed" | "failure" | "error" | "skipped"}}
"""

import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CALIBRATION = Path("/calibration")
workspace = Path(os.environ["WORKSPACE"])
known_files_path = CALIBRATION / "known_files.json"
known_files = json.loads(known_files_path.read_text()) if known_files_path.exists() else None
base = Path("/tmp/workspace_base")
if known_files is not None:
    shutil.copytree(workspace, base, symlinks=True)
results = {}

for variant in sorted((CALIBRATION / "variants").iterdir()):
    shutil.rmtree(workspace, ignore_errors=True)
    if known_files is None:
        shutil.copytree(variant, workspace)
    else:
        shutil.copytree(base, workspace, symlinks=True)
        for relative_path in known_files:
            (workspace / relative_path).unlink(missing_ok=True)
        shutil.copytree(variant, workspace, dirs_exist_ok=True)
    report = Path(f"/tmp/{variant.name}.xml")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--timeout=60",
            f"--junitxml={report}",
            str(CALIBRATION / "test_hidden.py"),
        ],
        cwd="/tmp",
        capture_output=True,
        timeout=900,
    )
    outcomes = {}
    if report.exists():
        for case in ET.parse(report).getroot().iter("testcase"):
            class_name = case.get("classname", "").split(".")[-1]
            test_name = (class_name + "::" if class_name.startswith("Test") else "") + case.get(
                "name"
            )
            outcomes[test_name] = next(
                (child.tag for child in case if child.tag in ("failure", "error", "skipped")),
                "passed",
            )
    results[variant.name] = outcomes

(CALIBRATION / "results.json").write_text(json.dumps(results, indent=1))
