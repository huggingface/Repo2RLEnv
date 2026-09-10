#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/seta/venv/bin:$PATH"
python - <<'PY'
import json,time
from pathlib import Path
path=Path('/work/seta/synth_data/unix_linux_se/unix-se-26047/synth_info.json')
deadline=time.monotonic()+1800
while time.monotonic()<deadline:
    info=json.loads(path.read_text())
    if info['status'] != 'in_progress':
        print(json.dumps(info,indent=2))
        if info.get('status')!='done' or info.get('verdict')!='PASS':
            raise SystemExit('Native synthesis did not accept the task; inspect the preserved attempt')
        break
    time.sleep(15)
else:
    raise SystemExit('Native generation is still active; no export taken from a moving task')
PY
python /work/recipes/runtime/harbor_artifacts.py export /work/seta/synth_data/unix_linux_se/unix-se-26047 /work/seta/harbor/unix-se-26047
python /work/recipes/runtime/harbor_artifacts.py audit /work/seta/harbor/unix-se-26047 /evidence/seta/harbor-smoke-01
