#!/usr/bin/env bash
set -euo pipefail
python - <<'PY'
from pathlib import Path
import re,json
for root in [Path('/work/seta/synth_data'), Path('/work/seta/evol_data')]:
  for log in root.rglob('*agent_log.txt'):
    lines=log.read_text(errors='replace').splitlines()
    results=[line for line in lines if line.startswith('ResultMessage')]
    tools=[line for line in lines if line.startswith('AssistantMessage') and 'ToolUseBlock(' in line]
    costs=[float(value) for line in results for value in re.findall(r'total_cost_usd=([0-9.]+)',line)]
    tool_names=[re.findall(r"name='([^']+)'",line)[:1] for line in tools[-3:]]
    print(json.dumps({'log':str(log),'bytes':log.stat().st_size,'result_messages':len(results),
                      'reported_usd':sum(costs),'recent_tools':tool_names}))
  for meta in root.rglob('*info.json'):
    print(meta,meta.read_text()[:1200])
PY
