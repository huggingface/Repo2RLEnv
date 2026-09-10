#!/usr/bin/env bash
set -euo pipefail
python - <<'PY'
from pathlib import Path
import re,json
for root in [Path('/work/seta/synth_data'), Path('/work/seta/evol_data')]:
  for log in root.rglob('*agent_log.txt'):
    lines=log.read_text(errors='replace').splitlines()
    useful=[line for line in lines if line.startswith(('AssistantMessage','ResultMessage'))]
    print(str(log), 'bytes',log.stat().st_size)
    for line in useful[-2:]: print(line[:1200])
  for meta in root.rglob('*info.json'):
    print(meta,meta.read_text()[:1200])
PY
