#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/endless/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime:/work/endless/upstream
export REPRO_API_RESPONSES=/evidence/endless/conversion-01-api.jsonl
export PYTHONUNBUFFERED=1
cd /work/endless/upstream
git apply /work/recipes/endless-terminals/patches/002-converter-receipts.patch
timeout --signal=TERM --kill-after=30 1800 python /work/recipes/endless-terminals/export_harbor.py /work/endless/native-batch-01 /work/endless/converted-batch-01 /work/endless/harbor
