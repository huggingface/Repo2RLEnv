#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/terminalworld/venv/bin:$PATH"
export PYTHONPATH=/work/terminalworld/upstream
export IS_SANDBOX=1
cd /work/terminalworld/upstream
mkdir -p .claude/skills
python - <<'PY'
import shutil, json
from pathlib import Path
for source, name in [('environment_building/skill','docker-env-builder'),('test_generation/skill','terminal-task-refiner')]:
    shutil.copytree(source,Path('.claude/skills')/name,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
Path('/evidence/terminalworld/skill-layout-compat.json').write_text(json.dumps({
    'change':'Copy released skill folders to the .claude/skills locations required by original agents',
    'content':'Original skill markdown and scripts, excluding Python bytecode',
},indent=2))
PY
python -m environment_building.build_environment \
 --recording-id 100135 --recordings-dir /work/terminalworld/native-tasks \
 --max-turns 30 --max-cost 1.5 --model claude-sonnet-4-6 \
 --log-dir /evidence/terminalworld/environment --run-mode retry_failed
test -s /work/terminalworld/native-tasks/100135/environment/Dockerfile
