#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/cli-gym/venv/bin:$PATH"
export PYTHONPATH=/work/cli-gym/upstream/src:/work/recipes/runtime
export LLM_MODEL=openai/gpt-4o
export OPENAI_API_BASE=https://api.openai.com/v1
export REPRO_API_RESPONSES=/evidence/cli-gym/generation-api.jsonl
cd /work/cli-gym/upstream
test ! -d CLI-Gym/destruction_tasks/addict
python /work/recipes/cli-gym/native_driver.py build cli-gym-addict-terminus-2 terminus-2 1 --no-run-terminal-bench
python - <<'PY'
from pathlib import Path
import json
tasks = list(Path('CLI-Gym/destruction_tasks/addict').glob('*/full_task.json'))
assert len(tasks) == 1
task = json.loads(tasks[0].read_text())
assert task['Selected UTs'] and task['Task Description']
print(json.dumps({'generated':1, 'path':str(tasks[0].parent)}))
PY
