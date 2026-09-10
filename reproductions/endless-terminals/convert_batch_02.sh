#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/endless/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime:/work/endless/upstream
export REPRO_API_RESPONSES=/evidence/endless/conversion-02-api.jsonl
export PYTHONUNBUFFERED=1
cd /work/endless/upstream
python /work/recipes/endless-terminals/export_harbor.py /work/endless/native-batch-02 /work/endless/converted-batch-02 /work/endless/harbor-batch-02
