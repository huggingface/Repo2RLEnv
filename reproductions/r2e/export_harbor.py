"""Convert a native R2E function/test record to a function-completion task."""

import ast
import json
import re
import shutil
from pathlib import Path

from harbor_artifacts import audit, hashes

ROOT = Path("/work/r2e")
source = Path("/root/buckets/r2e_bucket/execution/pilot_out.json")
records = json.loads(source.read_text())
assert len(records) == 1
record = records[0]
assert record["function_name"] == "ast_height"
context = re.search(r"```python\n(.*?)```", record["context"]["context"], re.S).group(1)
tree = ast.parse(context)
function = next(
    node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "ast_height"
)
docstring = ast.get_docstring(function)
original_body = list(function.body)
function.body = [
    *original_body[:1],
    ast.Raise(
        exc=ast.Call(func=ast.Name(id="NotImplementedError", ctx=ast.Load()), args=[], keywords=[])
    ),
]
baseline = ast.unparse(ast.fix_missing_locations(tree)) + "\n"
latest = record["test_history"]["history"][-1]
assert latest["exec_stats"]["coverage_logs"][-1]["branch_coverage_percentage"] >= 80
task = ROOT / "harbor/python-graphs-ast-height"
task.mkdir(parents=True, exist_ok=False)
for entry in ["environment", "tests", "solution"]:
    (task / entry).mkdir()
(task / "environment/fut_module.py").write_text(baseline)
(task / "instruction.md").write_text(
    "Implement `ast_height(ast_node)` in `/app/fut_module.py`. The input uses `gast` AST nodes. Preserve the function signature.\n\n"
    + docstring
    + "\n"
)
(task / "task.toml").write_text(
    'version = "1.0"\n[environment]\nnetwork_mode = "no-network"\n[verifier]\ntimeout_sec = 120\n[agent]\ntimeout_sec = 300\n'
)
(task / "environment/Dockerfile").write_text(
    "FROM python:3.11-slim\nRUN pip install --no-cache-dir gast==0.7.0\nWORKDIR /app\nCOPY fut_module.py /app/fut_module.py\n"
)
(task / "solution/fut_module.py").write_text(context)
(task / "solution/solve.sh").write_text(
    "#!/bin/bash\nset -euo pipefail\ncp /solution/fut_module.py /app/fut_module.py\n"
)
(task / "tests/reference_module.py").write_text(context)
assert len(latest["tests"]) == 1
(task / "tests/test_generated.py").write_text(next(iter(latest["tests"].values())))
shutil.copy2(ROOT / "upstream/LICENSE", task / "tests/LICENSE.upstream")
(task / "tests/run_tests.py").write_text("""import importlib.util, runpy, sys
sys.path.insert(0,'/app')
import fut_module
spec=importlib.util.spec_from_file_location('reference_module','/tests/reference_module.py')
reference=importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)
fut_module.reference_ast_height=reference.ast_height
runpy.run_path('/tests/test_generated.py',run_name='__main__')
""")
(task / "tests/test.sh").write_text("""#!/bin/bash
set -uo pipefail
mkdir -p /logs/verifier
if /usr/local/bin/python /tests/run_tests.py > /logs/verifier/tests.log 2>&1; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
""")
report = audit(task, ROOT / "audit" / task.name, ["nop", "oracle"])
output = ROOT / "native-output"
output.mkdir(exist_ok=True)
shutil.copy2(source, output / "pilot_out.json")
receipt = {
    "native_record": str(source),
    "task": str(task),
    "files": hashes(task),
    "native_coverage": latest["exec_stats"]["coverage_logs"],
    "native_test_results": latest["exec_stats"]["run_tests_logs"],
    "harbor": report,
    "training_approved": False,
    "conversion": [
        "Original sliced context with only target body removed",
        "Instruction from original docstring",
        "Unchanged generated unittest code",
        "Original reference source hidden until verification; native service normally provides a compiled reference",
    ],
    "known_limitations": [
        "The native test/reference API is exposed in the candidate module during grading; requires adversarial audit before training use",
        "One function from one repository does not reproduce full benchmark-scale coverage",
    ],
}
Path("/evidence/r2e/export-audit.json").write_text(json.dumps(receipt, indent=2))
print(json.dumps({"task": str(task), "contrast": report["execution_contrast_passed"]}))
