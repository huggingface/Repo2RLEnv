#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/control/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime
python - <<'PY'
from pathlib import Path
import json
from harbor_artifacts import export_native, audit
root = Path('/work/dataarc-terminal')
reports = []
for spec in sorted((root/'native-output/tasks').glob('*/task.toml')):
    destination = root/'harbor'/spec.parent.name
    receipt = export_native(spec.parent, destination)
    destination.with_suffix('.manifest.json').write_text(json.dumps(receipt, indent=2))
    reports.append(audit(destination, root/'audit'/destination.name, ['nop','oracle']))
assert reports
Path('/evidence/dataarc-terminal/audit-summary.json').write_text(json.dumps(reports, indent=2))
PY
