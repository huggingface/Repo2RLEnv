#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/swe-rebench-v2/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime
export REPRO_API_RESPONSES=/evidence/swe-rebench-v2/annotation-api.jsonl
cd /work/swe-rebench-v2/upstream
python /work/recipes/swe-rebench-v2/annotation_driver.py \
 --input /work/swe-rebench-v2/native-output/input.json \
 --output /work/swe-rebench-v2/native-output/annotated.json \
 --field both --send --max-workers 1 --model gpt-4o --api-base https://api.openai.com/v1
