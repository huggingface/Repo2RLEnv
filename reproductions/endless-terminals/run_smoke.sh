#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/endless/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime
export REPRO_API_RESPONSES=/evidence/endless/smoke-01-api.jsonl
export PYTHONUNBUFFERED=1
cd /work/endless/upstream
# Original CLI default model is gpt-4o; endpoint is the named deviation.
timeout --signal=TERM --kill-after=30 1800 python - <<'PY'
import random,runpy,sys
random.seed(20260910)
sys.argv=['generate_tasks.py','--num-tasks','1','--batch-size','1','--max-concurrency','1',
          '--model','gpt-4o','--out-dir','/work/endless/native-smoke-01','--verbose']
runpy.run_path('generate_tasks.py',run_name='__main__')
PY
