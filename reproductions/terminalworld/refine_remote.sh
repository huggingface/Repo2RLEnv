#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/terminalworld/venv/bin:$PATH"
export PYTHONPATH=/work/terminalworld/upstream
export IS_SANDBOX=1
cd /work/terminalworld/upstream
python - <<'PY'
import shutil
from pathlib import Path
source=Path('/work/terminalworld/native-tasks/100135')
shutil.copytree(source,Path('/work/terminalworld/native-drafts/100135'),ignore=shutil.ignore_patterns('.agent_workspace','__pycache__','*.pyc'))
PY
python -m test_generation.refine_task --task-id 100135 \
 --refined-tasks-dir /work/terminalworld/native-tasks --max-turns 40 --max-cost 3 \
 --model claude-sonnet-4-6 --log-dir /evidence/terminalworld/refine
test -s /work/terminalworld/native-tasks/100135/task.toml
