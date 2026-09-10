#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/tmax/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime
cd /work/tmax/upstream
git apply /work/recipes/tmax/patches/002-null-metadata.patch
python -m rl_data.scripts.analyze.convert_to_harbor --src /work/tmax/native-smoke-01 --dst /work/tmax/harbor-v2 --workers 1
python - <<'PY'
from pathlib import Path
from harbor.models.task.task import Task
tasks=list(Path('/work/tmax/harbor-v2').glob('*/task.toml'))
assert tasks
for p in tasks: Task(p.parent)
print('Schema-valid tasks:',len(tasks))
PY
