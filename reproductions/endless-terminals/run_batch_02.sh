#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/endless/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime
export REPRO_API_RESPONSES=/evidence/endless/batch-02-api.jsonl
export REPRO_BUILD_EVIDENCE=/evidence/endless/batch-02-builds
export PYTHONUNBUFFERED=1
cd /work/endless/upstream
timeout --signal=TERM --kill-after=30 1800 python - <<'PY'
import random,runpy,sys
random.seed(20260912)
sys.argv=['generate_tasks.py','--num-tasks','5','--batch-size','1','--max-concurrency','1',
          '--model','gpt-4o','--out-dir','/work/endless/native-batch-02','--verbose']
runpy.run_path('generate_tasks.py',run_name='__main__')
PY
