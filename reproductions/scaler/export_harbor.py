"""Harbor answer-file adapter preserving SCALER's native -1/+1 reward."""

import importlib.metadata
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

from harbor_artifacts import audit, hashes

ROOT = Path("/work/scaler")
EVIDENCE = Path("/evidence/scaler")
module_path = ROOT / "upstream/verl/utils/reward_score/think_test_math.py"
spec = importlib.util.spec_from_file_location("native_score", module_path)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
subprocess.run(["docker", "pull", "python:3.12-slim"], check=True)
image_data = json.loads(
    subprocess.check_output(["docker", "image", "inspect", "python:3.12-slim"], text=True)
)
image = image_data[0]["RepoDigests"][0]
(EVIDENCE / "harbor-image.json").write_text(json.dumps(image_data, indent=2))
requirements = (
    "\n".join(
        f"{name}=={importlib.metadata.version(name)}"
        for name in [
            "math-verify",
            "latex2sympy2-extended",
            "antlr4-python3-runtime",
            "sympy",
            "mpmath",
        ]
    )
    + "\n"
)
rows = json.loads((ROOT / "native-output/instances.json").read_text())
reports = []
for row in rows:
    task = ROOT / "harbor" / f"abbreviation-d{row['difficulty']}"
    task.mkdir(parents=True, exist_ok=False)
    for entry in ["environment", "tests", "solution"]:
        (task / entry).mkdir()
    (task / "instruction.md").write_text(
        row["instruction"]
        + "\nWrite your final answer to `/app/answer.txt`, using the boxed answer format described above.\n"
    )
    (task / "task.toml").write_text(
        'version = "1.0"\n[environment]\nnetwork_mode = "no-network"\n[verifier]\ntimeout_sec = 120\n[agent]\ntimeout_sec = 300\n'
    )
    (task / "environment/requirements.txt").write_text(requirements)
    (task / "environment/Dockerfile").write_text(
        f"FROM {image}\nCOPY requirements.txt /tmp/requirements.txt\nRUN pip install --no-cache-dir -r /tmp/requirements.txt\nWORKDIR /app\n"
    )
    shutil.copy2(module_path, task / "tests/native_score.py")
    shutil.copy2(ROOT / "upstream/LICENSE", task / "tests/LICENSE.upstream")
    (task / "tests/reference.json").write_text(json.dumps(row["reference_answer"]))
    (task / "tests/grade.py").write_text("""import json
from pathlib import Path
from native_score import compute_score
answer=Path('/app/answer.txt')
result=compute_score(answer.read_text() if answer.exists() else '',json.loads(Path('/tests/reference.json').read_text()))
Path('/logs/verifier/native-score.json').write_text(json.dumps(result))
Path('/logs/verifier/reward.txt').write_text(str(result['score']))
""")
    (task / "tests/test.sh").write_text(
        "#!/bin/bash\nset -euo pipefail\nmkdir -p /logs/verifier\n/usr/local/bin/python /tests/grade.py\n"
    )
    (task / "solution/solve.sh").write_text(
        "#!/bin/bash\nset -euo pipefail\ncat > /app/answer.txt <<'SCALER_REFERENCE'\n"
        + row["reference_answer"]
        + "\nSCALER_REFERENCE\n"
    )
    native_nop = native.compute_score("", row["reference_answer"])
    native_oracle = native.compute_score(row["reference_answer"], row["reference_answer"])
    report = audit(
        task, ROOT / "audit" / task.name, ["nop", "oracle"], nop_reward=-1, oracle_reward=1
    )
    reports.append(
        {
            "family": row["family"],
            "task": str(task),
            "files": hashes(task),
            "native_nop": native_nop,
            "native_oracle": native_oracle,
            "harbor": report,
            "conversion": "Native concrete-instance prompt plus answer-file interface; unchanged score function and -1/+1 rewards",
            "oracle_kind": "Reference answer computed by original SandboxFusion reference program; answer replay for the fixed instance",
            "training_approved": False,
        }
    )
    (EVIDENCE / "export-audit.json").write_text(json.dumps(reports, indent=2))
print(json.dumps({"exports": len(reports), "native_reward_scale": [-1, 1]}))
