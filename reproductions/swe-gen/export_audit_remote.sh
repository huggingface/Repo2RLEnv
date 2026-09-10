#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/control/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime
python - <<'PY'
from pathlib import Path
import json
from harbor_artifacts import export_native, audit
from harbor.models.task.task import Task
source = Path('/work/swe-gen/native-tasks/axios__axios-7150')
target = Path('/work/swe-gen/harbor/axios-7150')
receipt = export_native(source,target)
receipt['source_task_name'] = source.name
receipt['export_task_name'] = target.name
receipt['task_name_compatibility'] = 'Docker-safe directory name; native file bytes preserved'
receipt['resolved_environment'] = Task(target).config.environment.model_dump(mode='json')
target.with_suffix('.manifest.json').write_text(json.dumps(receipt,indent=2))
report = audit(target,Path('/work/swe-gen/audit/axios-7150'),['nop','oracle'])
Path('/evidence/swe-gen/export-audit.json').write_text(json.dumps({'export':receipt,'audit':report},indent=2))
PY
