#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/tmax/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime:/work/tmax/upstream
export REPRO_API_RESPONSES=/evidence/tmax/probe-02-api.jsonl
export PYTHONUNBUFFERED=1
cd /work/tmax/upstream
task=/work/tmax/native-smoke-01/task_000000_917b92fc
harbor_task=/work/tmax/harbor-v2/task_000000_917b92fc
python /work/recipes/runtime/native_terminal_probe.py tmax "$task" /evidence/tmax/probe-02/task_000000_917b92fc --model anthropic/claude-sonnet-4-6 --harbor-task "$harbor_task" --max-actions 40
python /work/recipes/runtime/harbor_artifacts.py audit "$harbor_task" /evidence/tmax/probe-02-harbor/task_000000_917b92fc
