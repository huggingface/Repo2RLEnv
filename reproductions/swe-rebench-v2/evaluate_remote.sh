#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/swe-rebench-v2/venv/bin:$PATH"
cd /work/swe-rebench-v2/upstream
python scripts/eval.py --json /work/swe-rebench-v2/native-output/input.json \
 --max-workers 1 --golden-eval \
 --report-json /work/swe-rebench-v2/native-output/golden-evaluation.json
cp -R logs /work/swe-rebench-v2/native-output/native-logs
