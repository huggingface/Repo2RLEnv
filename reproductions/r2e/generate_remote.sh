#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/r2e/upstream/.venv/bin:$PATH"
export OPENAI_KEY="$OPENAI_API_KEY"
export PYTHONPATH=/work/recipes/runtime
export REPRO_API_RESPONSES=/evidence/r2e/generation-api.jsonl
cd /work/r2e/upstream
python /work/recipes/r2e/native_driver.py genexec -e pilot \
 --in-file pilot_extracted.json --function ast_height --max_rounds 3 \
 --local --model_name gpt-4o --max_tokens 1024 --openai_timeout 90 \
 --multiprocess 1 --execution-multiprocess 1 --save_chat
