"""Export native SWE-Flow skeleton commits and references; execute remotely."""

from __future__ import annotations

import io
import json
import re
import shlex
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

from harbor.models.task.task import Task
from harbor_artifacts import audit, hashes
from sweflow.extensions.python.helper import generate_test_script

ROOT = Path("/work/swe-flow")
OUTPUT = ROOT / "native-output"
EXPORT = ROOT / "exports/export-v3"
IMAGE = "hambaobao/sweflow@sha256:479b76e5ed273a578abfb31554db3c5bd33fb656a3f3925110fae2560b628df0"

codebase = EXPORT / "codebase"
if codebase.exists():
    raise FileExistsError(codebase)
with zipfile.ZipFile(OUTPUT / "codebase.zip") as archive:
    for entry in archive.infolist():
        path = Path(entry.filename)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Unsafe native archive path: {path}")
    archive.extractall(codebase)

rows = [json.loads(line) for line in (OUTPUT / "dataset.jsonl").read_text().splitlines()]
records = []
for row in rows:
    # Docker-derived names cannot contain the native __--__ repository separator.
    slug = re.sub(r"[^a-z0-9]+", "-", row["instance_id"].lower()).strip("-")
    task = EXPORT / "harbor" / slug
    if task.exists():
        raise FileExistsError(task)
    workspace = task / "environment/workspace"
    workspace.mkdir(parents=True)
    payload = subprocess.check_output(["git", "-C", str(codebase), "archive", row["base_commit"]])
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        archive.extractall(workspace, filter="data")
    (task / "tests/pristine").mkdir(parents=True)
    shutil.move(str(workspace / "tests"), str(task / "tests/pristine/tests"))
    (task / "solution").mkdir()
    (task / "instruction.md").write_text(row["problem_statement"])
    (task / "solution/reference.patch").write_text(row["patch"] + "\n")
    (task / "solution/solve.sh").write_text(
        "#!/bin/bash\nset -euo pipefail\ncd /workspace\ngit apply --inaccurate-eof -p0 /solution/reference.patch\n"
    )
    (task / "environment/Dockerfile").write_text(
        f"FROM {IMAGE}\nUSER root\n"
        "RUN python -m pip uninstall -y spectree && rm -rf /sweflow /workspace /root/.cache/pip\n"
        "COPY workspace /workspace\nWORKDIR /workspace\nENV PYTHONPATH=/workspace\n"
    )
    required = row["fail_to_pass"] + row["pass_to_pass"]
    assert row["fail_to_pass"]
    native_command = generate_test_script(required)
    (task / "tests/native-test-command.sh").write_text("#!/bin/bash\n" + native_command + "\n")
    (task / "tests/test.sh").write_text("""#!/bin/bash
set -uo pipefail
mkdir -p /logs/verifier
echo 0 > /logs/verifier/reward.txt
cd /workspace || exit 1
cp -a /tests/pristine/. /workspace/ || exit 1
if bash /tests/native-test-command.sh > /logs/verifier/test-stdout.txt 2>&1; then
  echo 1 > /logs/verifier/reward.txt
fi
""")
    (task / "task.toml").write_text("""version = "1.0"
[metadata]
source = "SWE-Flow native development schedule"
[agent]
timeout_sec = 600
[verifier]
timeout_sec = 180
[environment]
build_timeout_sec = 600
cpus = 1
memory_mb = 2048
allow_internet = false
""")
    Task(task)
    # Native execution contrast uses the same published runtime and original
    # test command, independently of Harbor's reward-file integration.
    native = {}
    patch_check = None
    for mode in ["nop", "oracle"]:
        source = EXPORT / "native-probe" / row["instance_id"] / mode
        shutil.copytree(workspace, source)
        shutil.copytree(task / "tests/pristine", source, dirs_exist_ok=True)
        if mode == "oracle":
            check = subprocess.run(
                ["git", "apply", "--check", "-p0", str(task / "solution/reference.patch")],
                cwd=source,
                capture_output=True,
                text=True,
            )
            patch_check = {
                "default_returncode": check.returncode,
                "stderr": check.stderr,
                "compatibility": "--inaccurate-eof for upstream diffs missing end-of-file newline markers",
            }
            subprocess.run(
                ["git", "apply", "--inaccurate-eof", "-p0", str(task / "solution/reference.patch")],
                cwd=source,
                check=True,
            )
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--cpus",
            "1",
            "--memory",
            "2g",
            "-v",
            f"{source}:/workspace",
            "-w",
            "/workspace",
            "-e",
            "PYTHONPATH=/workspace",
            "--entrypoint",
            "/bin/bash",
            IMAGE,
            "-lc",
            native_command,
        ]
        output = EXPORT / "native-probe" / row["instance_id"] / (mode + ".log")
        with output.open("w") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=240)
        native[mode] = {
            "returncode": result.returncode,
            "command": shlex.join(command),
            "log": str(output),
        }
    record = {
        "instance_id": row["instance_id"],
        "source_row": row,
        "native": native,
        "files": hashes(task),
        "image": IMAGE,
        "isolation_changes": [
            "hide test tree until verification",
            "remove repository history",
            "remove installed healthy spectree and source backups",
            "disable solver internet",
        ],
        "patch_application": patch_check,
        "quality_approved": False,
    }
    (task.parent / (task.name + ".manifest.json")).write_text(json.dumps(record, indent=2))
    record["harbor"] = audit(task, EXPORT / "audit" / task.name, ["nop", "oracle"])
    parity = []
    for mode in ["nop", "oracle"]:
        run = record["harbor"]["runs"][mode]
        trials = run.get("trials", [])
        valid = (
            run.get("returncode") == 0 and len(trials) == 1 and not trials[0].get("exception_info")
        )
        reward = (
            ((trials[0].get("verifier_result") or {}).get("rewards") or {}).get("reward")
            if valid
            else None
        )
        parity.append(reward in (0, 1) and (native[mode]["returncode"] == 0) == (reward == 1))
    record["reward_parity"] = all(parity)
    records.append(record)
    Path("/evidence/swe-flow/export-audit-v3.json").write_text(json.dumps(records, indent=2))
