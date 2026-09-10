#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/swe-gen/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime
export REPRO_API_RESPONSES=/evidence/swe-gen/instruction-api.jsonl
export IS_SANDBOX=1
cd /work/swe-gen/upstream
python /work/recipes/swe-gen/native_driver.py create --repo axios/axios --pr 7150 \
  --output /work/swe-gen/native-tasks --state-dir /work/swe-gen/state \
  --cc-timeout 1500 -e docker
python - <<'PY'
from pathlib import Path
import json
specs = list(Path('/work/swe-gen/native-tasks').glob('*/task.toml'))
Path('/evidence/swe-gen/generation-summary.json').write_text(json.dumps({'native_emitted':len(specs), 'input':'axios/axios#7150', 'native_defaults_preserved':True, 'sdk_budget_usd':8, 'sdk_max_turns':80}, indent=2))
assert specs, 'No native task emitted; inspect preserved native failure'
PY
