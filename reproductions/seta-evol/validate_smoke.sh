#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/seta/venv/bin:$PATH"
python - <<'PY'
import json
info=json.load(open('/work/seta/evol_data/unix-se-26047__b1/synth_info.json'))
assert info['status']=='done' and info['verdict']=='PASS', info
PY
python /work/recipes/runtime/harbor_artifacts.py export /work/seta/evol_data/unix-se-26047__b1 /work/seta/evol-harbor/unix-se-26047__b1
python /work/recipes/runtime/harbor_artifacts.py audit /work/seta/evol-harbor/unix-se-26047__b1 /evidence/seta-evol/harbor-smoke-01
