#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/tmax/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime
export REPRO_API_RESPONSES=/evidence/tmax/smoke-01-api.jsonl
export PYTHONUNBUFFERED=1
cd /work/tmax/upstream
timeout --signal=TERM --kill-after=30 2400 python - <<'PY'
import random,runpy,sys
random.seed(20260910)
sys.argv=['generate_tasks.py','--num-tasks','1','--batch-size','1','--max-concurrency','1',
          '--model','anthropic/claude-sonnet-4-6','--out-dir','/work/tmax/native-smoke-01',
          '--corpus-kind','legacy','--verbose']
runpy.run_module('rl_data.generate_tasks',run_name='__main__')
PY
