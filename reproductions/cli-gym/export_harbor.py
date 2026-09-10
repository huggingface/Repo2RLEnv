"""Translate native CLI-Gym problem instances without repairing native grading."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml
from harbor_artifacts import audit, hashes

ROOT = Path("/work/cli-gym")
UPSTREAM = ROOT / "upstream"
BASE = "swebench/swesmith.x86_64.mewwts_1776_addict.75284f95@sha256:f8650aba9f52e514b3adf48c0c1855285179754466d742785c21f69da2da0a8e"
GRADER = """from pathlib import Path
import json
from terminal_bench.parsers.pytest_parser import PytestParser
from terminal_bench.parsers.base_parser import UnitTestStatus
try:
    parsed = PytestParser().parse(Path('/logs/verifier/native-stdout.txt').read_text())
except Exception as error:
    parsed = None
    Path('/logs/verifier/parser-error.txt').write_text(str(error))
# Exact upstream Harness._is_resolved behavior, including empty-result semantics.
reward = parsed is not None and all(x == UnitTestStatus.PASSED for x in parsed.values())
Path('/logs/verifier/reward.txt').write_text('1' if reward else '0')
Path('/logs/verifier/parsed.json').write_text(json.dumps({k:v.value for k,v in (parsed or {}).items()}))
"""
records = []
for source in sorted((UPSTREAM / "CLI-Gym/problem_instances/addict").iterdir()):
    config = yaml.safe_load((source / "task.yaml").read_text())
    task = ROOT / "harbor-v2" / source.name.replace(".", "-")
    task.mkdir(parents=True)
    (task / "environment").mkdir()
    (task / "tests").mkdir()
    canary = (source / "task.yaml").read_text().splitlines()[0]
    (task / "instruction.md").write_text(f"<!-- {canary} -->\n\n" + config["instruction"])
    runtime = (UPSTREAM / "CLI-Gym/build_agent_runtime_image/Dockerfile.terminus-2").read_text()
    runtime = runtime.replace("FROM {docker_image_name}", f"FROM {BASE} AS cli-gym-runtime")
    original = (source / "Dockerfile").read_text()
    dockerfile = (
        runtime
        + "\n"
        + original.replace("FROM cli-gym-addict-terminus-2:latest", "FROM cli-gym-runtime")
    )
    dockerfile += "\nRUN rm -rf /testbed/.git\nWORKDIR /testbed\n"
    (task / "environment/Dockerfile").write_text(dockerfile)
    shutil.copy2(source / "run-tests.sh", task / "tests/native-run-tests.sh")
    for relative in ["parsers/pytest_parser.py", "parsers/base_parser.py", "utils/logger.py"]:
        destination = task / "tests/native/terminal_bench" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(UPSTREAM / "src/terminal_bench" / relative, destination)
    for directory in ["terminal_bench", "terminal_bench/parsers", "terminal_bench/utils"]:
        (task / "tests/native" / directory / "__init__.py").touch()
    shutil.copy2(UPSTREAM / "LICENSE", task / "tests/native/LICENSE")
    (task / "tests/grade.py").write_text(GRADER)
    (task / "tests/test.sh").write_text("""#!/bin/bash
set -uo pipefail
mkdir -p /logs/verifier
echo 0 > /logs/verifier/reward.txt
cd /testbed || exit 1
bash /tests/native-run-tests.sh > /logs/verifier/native-stdout.txt 2>&1
PYTHONPATH=/tests/native python /tests/grade.py
""")
    (task / "task.toml").write_text("""version = "1.0"
[metadata]
source = "CLI-Gym original assembly; restoration oracle absent upstream"
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
    record = {
        "native_instance": str(source),
        "variant": "hard" if source.name.endswith(".hard") else "hint",
        "shared_environment": "configuration_key_confusion",
        "native_dockerfile": original,
        "export": str(task),
        "files": hashes(task),
        "oracle": "missing_upstream",
        "compatibility": [
            "inline pinned native runtime build",
            "remove git metadata",
            "disable solver internet",
        ],
        "training_approved": False,
    }
    # Replay native run-tests.sh before the Harbor reward integration.
    native_log = ROOT / (source.name + ".native-nop.log")
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
        f"{source}:/native:ro",
        "-w",
        "/testbed",
        "--entrypoint",
        "bash",
        "cli-gym-addict-terminus-2:latest",
        "/native/run-tests.sh",
    ]
    with native_log.open("w") as log:
        process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=240)
    sys.path.insert(0, str(task / "tests/native"))
    from terminal_bench.parsers.base_parser import UnitTestStatus
    from terminal_bench.parsers.pytest_parser import PytestParser

    try:
        parsed = PytestParser().parse(native_log.read_text())
        reward = all(x == UnitTestStatus.PASSED for x in parsed.values())
        parser_error = None
    except Exception as error:
        reward = False
        parser_error = str(error)
    record["native_nop"] = {
        "returncode": process.returncode,
        "reward": int(reward),
        "parser_error": parser_error,
    }
    record["harbor"] = audit(task, ROOT / "audit" / task.name, ["nop", "oracle"])
    (task.parent / (task.name + ".manifest.json")).write_text(json.dumps(record, indent=2))
    records.append(record)
    Path("/evidence/cli-gym/export-audit.json").write_text(json.dumps(records, indent=2))
