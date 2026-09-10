#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/seta/venv/bin:$PATH"
export REPRO_AGENT_BUDGET_USD=8
export REPRO_AGENT_MAX_TURNS=100
export PYTHONUNBUFFERED=1
python - <<'PY'
from pathlib import Path
Path('/work/seta/repeat.csv').write_text('source,task_id\nunix_linux_se,unix-se-73498\nunix_linux_se,unix-se-52313\n')
PY
cd /work/seta/upstream/datasynth/seed2synth_pipeline
python run_orchestrator.py /work/recipes/seta-seed2synth/config/repeat.yaml --dry-run
timeout --signal=TERM --kill-after=30 3000 python run_orchestrator.py /work/recipes/seta-seed2synth/config/repeat.yaml --synth-only
