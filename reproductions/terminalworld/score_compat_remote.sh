#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/terminalworld/venv/bin:$PATH"
export PYTHONPATH=/work/terminalworld/upstream:/work/terminalworld/upstream/data_filtering:/work/terminalworld/upstream/environment_building:/work/recipes/runtime
export REPRO_API_RESPONSES=/evidence/terminalworld/score-compat-api.jsonl
python /work/recipes/terminalworld/score_compat.py \
  --recordings-dir /work/terminalworld/native-tasks --limit 1 \
  --output /evidence/terminalworld/value-scores-compat.json \
  --log-file /evidence/terminalworld/value-compat.log
