#!/usr/bin/env bash
set -euo pipefail
python - <<'PY'
import json
from pathlib import Path
for p in sorted(Path('/evidence').rglob('audit.json')):
    try: d=json.loads(p.read_text())
    except json.JSONDecodeError: continue
    print(json.dumps({'audit':str(p),'contrast':d['execution_contrast_passed'],'runs':{a:[{'rewards':(r.get('verifier_result') or {}).get('rewards'),'exception':(r.get('exception_info') or {}).get('exception_type')} for r in v.get('trials',[])] for a,v in d['runs'].items()}}))
for p in sorted(Path('/evidence').rglob('native-solutions.json')):
    d=json.loads(p.read_text()); print(json.dumps({'solver':str(p),'successes':d.get('num_success'),'runs':d.get('num_runs')}))
PY
