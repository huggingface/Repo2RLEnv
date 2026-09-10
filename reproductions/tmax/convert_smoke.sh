#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/tmax/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime
cd /work/tmax/upstream
python -m rl_data.scripts.analyze.convert_to_harbor --src /work/tmax/native-smoke-01 --dst /work/tmax/harbor --workers 1
python - <<'PY'
from pathlib import Path
from harbor.models.task.task import Task
tasks=list(Path('/work/tmax/harbor').glob('*/task.toml'))
assert tasks, 'Native generation/conversion produced no Harbor tasks'
for p in tasks:
    Task(p.parent)
    print('Schema valid:',p.parent)
PY
