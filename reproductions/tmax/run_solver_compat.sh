#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/tmax/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime:/work/tmax/upstream
export REPRO_API_RESPONSES=/evidence/tmax/solver-compat-01-api.jsonl
export REPRO_REQUIRE_TOOL_CALL=1
export PYTHONUNBUFFERED=1
cd /work/tmax/upstream
git apply /work/recipes/tmax/patches/003-solver-provider-compat.patch
python /work/recipes/runtime/native_terminal_probe.py tmax /work/tmax/runtime-fixed/task_000000_917b92fc /evidence/tmax/solver-compat-01 --model anthropic/claude-sonnet-4-6 --max-actions 40
