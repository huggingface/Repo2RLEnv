#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/secverifier/upstream/.venv/bin:$PATH"
export PYTHONPATH=/work/secverifier/upstream:/work/recipes/runtime
export REPRO_API_RESPONSES=/evidence/secverifier/generation-api.jsonl
cd /work/secverifier/upstream
cat > config.toml <<'TOML'
[llm.pilot]
model = "gpt-4o"
max_output_tokens = 4096
num_retries = 1
timeout = 120
temperature = 0.0
TOML
python /work/recipes/secverifier/native_driver.py --llm-config llm.pilot \
 --instance-id njs.cve-2022-32414 --dataset-name SEC-bench/Seed --label cve \
 --iterations 30 --max-budget-per-task 5 --num-workers 1 --headless \
 --output-dir /work/secverifier/native-output
