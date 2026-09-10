#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/control/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime
python - <<'PY'
import json
import subprocess
import tomllib
from pathlib import Path
from harbor_artifacts import audit, export_native

source = Path('/work/terminalworld/native-tasks/100135')
target = Path('/work/terminalworld/harbor/100135')
report = export_native(source, target)
report['native_refinement'] = json.loads((source / 'refinement_metadata.json').read_text())
image = tomllib.loads((target / 'task.toml').read_text())['environment']['docker_image']
# Rebuild from the final exported context: the refiner may have changed files
# after the earlier cached environment was built.
with Path('/evidence/terminalworld/export-build.log').open('w') as log:
    subprocess.run(['docker', 'build', '--progress=plain', '-t', image,
                    str(target / 'environment')], check=True, stdout=log,
                   stderr=subprocess.STDOUT, timeout=900)
report['final_image'] = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image]))
report['harbor'] = audit(target, Path('/work/terminalworld/audit/100135'), ['nop', 'oracle'])
report['training_approved'] = False
report['known_limitations'] = [
    'Native task permits internet; offline solver packaging has not been validated',
    'Native builder adapted an unavailable historical URL using another NASA URL present in source metadata',
    'Native refinement and three partial-solution checks do not constitute an independent adversarial audit',
]
Path('/evidence/terminalworld/export-audit.json').write_text(json.dumps(report, indent=2))
print(json.dumps({'native_status': report['native_refinement']['status'],
                  'execution_contrast_passed': report['harbor']['execution_contrast_passed']}))
PY
