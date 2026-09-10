#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/terminalworld/venv/bin:$PATH"
export PYTHONPATH=/work/terminalworld/upstream:/work/recipes/runtime
export REPRO_API_RESPONSES=/evidence/terminalworld/instruction-api.jsonl
python - <<'PY'
import json
from pathlib import Path
score=json.loads(Path('/evidence/terminalworld/value-scores-compat.json').read_text())['100135']
assert score['decision'].startswith('ACCEPT_'), score['decision']
filters=json.loads(Path('/evidence/terminalworld/input-filters.json').read_text())
record=next(x for x in filters if x['id']=='100135')
Path('/work/terminalworld/native-tasks/100135/source/analysis.json').write_text(json.dumps({'tui_classification':record['tui'],'external_urls':record['urls']['external_urls']},indent=2))
PY
python /work/recipes/terminalworld/native_driver.py task_synthesis.generate_instruction \
 --tasks-dir /work/terminalworld/native-tasks --ids 100135 --workers 1 \
 --log-file /evidence/terminalworld/instruction.log
test -s /work/terminalworld/native-tasks/100135/instruction.md
