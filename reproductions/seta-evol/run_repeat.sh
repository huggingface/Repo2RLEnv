#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/seta/venv/bin:$PATH"
export REPRO_AGENT_BUDGET_USD=8
export REPRO_AGENT_MAX_TURNS=100
export PYTHONUNBUFFERED=1
for task in unix-se-73498 unix-se-52313; do
    python /work/recipes/runtime/harbor_artifacts.py export "/work/seta/synth_data/unix_linux_se/$task" "/work/seta/harbor/$task"
    python /work/recipes/runtime/harbor_artifacts.py audit "/work/seta/harbor/$task" "/evidence/seta/harbor-repeat-01/$task"
done
python - <<'PY'
import json,shutil
from pathlib import Path
for task in ['unix-se-73498','unix-se-52313']:
    assert json.load(open(f'/work/seta/synth_data/unix_linux_se/{task}/synth_info.json'))['verdict']=='PASS'
    assert json.load(open(f'/evidence/seta/harbor-repeat-01/{task}/audit.json'))['execution_contrast_passed']
    shutil.copytree(Path('/work/seta/harbor')/task,Path('/work/seta/evol-repeat-input')/task)
PY
cd /work/seta/upstream/datasynth/evol_pipeline
python run_evol_orchestrator.py /work/recipes/seta-evol/config/repeat.yaml evolve --dry-run
timeout --signal=TERM --kill-after=30 3000 python run_evol_orchestrator.py /work/recipes/seta-evol/config/repeat.yaml evolve
