#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/cli-gym/venv/bin:$PATH"
export PYTHONPATH=/work/cli-gym/upstream/src:/work/recipes/runtime
export LLM_MODEL=openai/gpt-4o
export OPENAI_API_BASE=https://api.openai.com/v1
export REPRO_API_RESPONSES=/evidence/cli-gym/inversion-api.jsonl
cd /work/cli-gym/upstream
python /work/recipes/cli-gym/native_driver.py run ./CLI-Gym/destruction_tasks/addict terminus-2 --max-episodes 30 --n-concurrent 1 --n-attempts 1 --output /work/cli-gym/native-runs
python /work/recipes/cli-gym/native_driver.py assemble
