#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/terminalworld/venv/bin:$PATH"
export PYTHONPATH=/work/terminalworld/upstream
export IS_SANDBOX=1
cd /work/terminalworld/upstream
test -s /work/terminalworld/native-tasks/100135/refinement_metadata.json
python - <<'PY'
from pathlib import Path
import shutil
root = Path('/work/terminalworld/native-tasks/100135')
snapshot = Path('/evidence/terminalworld/pre-repair')
snapshot.mkdir(exist_ok=False)
for name in ['refinement_metadata.json', 'diagnosis_static.json', 'oracle_trial.json', 'nop_trial.json']:
    if (root / name).exists():
        shutil.copy2(root / name, snapshot / name)
PY
python -m test_generation.refine_task --task-id 100135 \
 --refined-tasks-dir /work/terminalworld/native-tasks --max-turns 60 --max-cost 3 \
 --model claude-sonnet-4-6 --repair --log-dir /evidence/terminalworld/repair
test -s /work/terminalworld/native-tasks/100135/task.toml
