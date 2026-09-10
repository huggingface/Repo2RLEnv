"""Package validated upstream mutations, tests and inverse reference into Harbor."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from harbor.models.task.task import Task
from swesmith.profiles import registry

TEST_SH = """#!/bin/bash
set -uo pipefail
mkdir -p /logs/verifier
echo 0 > /logs/verifier/reward.txt
cd /testbed
cp -a /tests/pristine/. /testbed/ || exit 1
source /opt/miniconda3/bin/activate || exit 1
conda activate testbed || exit 1
bash /tests/native-test-command.sh > /logs/verifier/test-stdout.txt 2>&1
python /tests/grade.py
"""

GRADER = """from pathlib import Path
import json,re
expected=json.loads(Path('/tests/expected.json').read_text())
text=Path('/logs/verifier/test-stdout.txt').read_text()
statuses={}
for line in text.splitlines():
    match=re.match(r'^(\\S+)(\\s+)(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\\b',line)
    if match: statuses[match.group(1)]=match.group(3)
# Match PythonProfile.log_parser and grading.test_passed at the pinned source revision.
required=expected['FAIL_TO_PASS']+expected['PASS_TO_PASS']
passed=bool(expected['FAIL_TO_PASS']) and all(statuses.get(case) in {'PASSED','XFAIL'} for case in required)
Path('/logs/verifier/test-statuses.json').write_text(json.dumps(statuses,indent=2))
Path('/logs/verifier/reward.txt').write_text('1' if passed else '0')
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instances", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    records = []
    for instance in json.loads(args.instances.read_text()):
        task = args.destination / instance["instance_id"].replace(".", "-")
        if task.exists():
            raise FileExistsError(task)
        for folder in ("environment", "tests/pristine", "solution"):
            (task / folder).mkdir(parents=True, exist_ok=True)
        profile = registry.get_from_inst(instance)
        image = instance["image_name"]
        inspect = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
        digest = inspect.get("RepoDigests", [])[0]
        mutation = instance["patch"]
        (task / "environment/mutation.patch").write_text(mutation)
        (task / "environment/Dockerfile").write_text(
            f"# syntax=docker/dockerfile:1\nFROM {digest}\nWORKDIR /testbed\n"
            "RUN --mount=type=bind,source=mutation.patch,target=/tmp/mutation.patch "
            "git apply /tmp/mutation.patch && rm -rf /testbed/.git\n"
        )
        (task / "instruction.md").write_text(instance["problem_statement"])
        (task / "solution/mutation.patch").write_text(mutation)
        (task / "solution/solve.sh").write_text(
            "#!/bin/bash\nset -euo pipefail\ncd /testbed\ngit apply -R /solution/mutation.patch\n"
        )
        test_command, _ = profile.get_test_cmd(instance)
        (task / "tests/native-test-command.sh").write_text("#!/bin/bash\n" + test_command + "\n")
        (task / "tests/test.sh").write_text(TEST_SH)
        (task / "tests/grade.py").write_text(GRADER)
        (task / "tests/expected.json").write_text(
            json.dumps({key: instance[key] for key in ("FAIL_TO_PASS", "PASS_TO_PASS")}, indent=2)
        )
        container = subprocess.check_output(["docker", "create", image], text=True).strip()
        try:
            f2p, p2p = profile.get_test_files(instance)
            for filename in sorted(set(f2p + p2p)):
                path = Path(filename)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError(f"Unexpected native test path: {filename}")
                target = task / "tests/pristine" / path
                target.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    ["docker", "cp", f"{container}:/testbed/{filename}", str(target)], check=True
                )
        finally:
            subprocess.run(["docker", "rm", container], check=True, stdout=subprocess.DEVNULL)
        (task / "task.toml").write_text("""version = "1.0"
[metadata]
source = "SWE-smith procedural mutation; pinned upstream reproduction"
[agent]
timeout_sec = 600
[verifier]
timeout_sec = 120
[environment]
build_timeout_sec = 600
cpus = 1
memory_mb = 2048
allow_internet = false
""")
        Task(task)
        records.append(
            {
                "instance_id": instance["instance_id"],
                "harbor": str(task),
                "image_digest": digest,
                "reference": "inverse original mutation",
                "isolation_deviation": "remove git metadata and disable solver internet",
                "test_command": test_command,
            }
        )
    args.destination.mkdir(parents=True, exist_ok=True)
    (args.destination / "export-manifest.json").write_text(json.dumps(records, indent=2))
    print(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()
