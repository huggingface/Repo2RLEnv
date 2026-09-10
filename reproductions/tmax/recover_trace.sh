#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/tmax/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime:/work/tmax/upstream
export PYTHONUNBUFFERED=1
cd /work/tmax/upstream
python /work/recipes/runtime/replay_native_calls.py /work/tmax/native-smoke-01/task_000000_917b92fc /evidence/tmax/probe-02-api.jsonl /evidence/tmax/recovered-replay-01 /work/tmax/harbor-v2/task_000000_917b92fc
python /work/recipes/runtime/harbor_artifacts.py audit /work/tmax/harbor-v2/task_000000_917b92fc /evidence/tmax/recovered-harbor-01
