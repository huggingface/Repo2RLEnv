#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/terminalworld/venv/bin:$PATH"
export PYTHONPATH=/work/terminalworld/upstream:/work/terminalworld/upstream/data_filtering:/work/recipes/runtime
export REPRO_API_RESPONSES=/evidence/terminalworld/extract-api.jsonl
python - <<'PY'
import json, shutil
from pathlib import Path
source = Path('/work/terminalworld/clean-recordings/100135')
target = Path('/work/terminalworld/native-tasks/100135/source')
shutil.copytree(source, target)
filters = json.loads(Path('/evidence/terminalworld/input-filters.json').read_text())
record = next(x for x in filters if x['id']=='100135')
(target/'analysis.json').write_text(json.dumps({'tui_classification': record['tui'], 'external_urls':record['urls']},indent=2))
PY
python /work/recipes/terminalworld/native_driver.py task_synthesis.extract_solution \
  --tasks-dir /work/terminalworld/native-tasks --ids 100135 --workers 1 \
  --log-file /evidence/terminalworld/extract.log
test -s /work/terminalworld/native-tasks/100135/solution/solve.sh
# The released value scorer consumes solution/solve.sh, despite README stage order.
python /work/recipes/terminalworld/native_driver.py data_filtering.score_value \
  --recordings-dir /work/terminalworld/native-tasks --limit 1 \
  --output /evidence/terminalworld/value-scores-after-extraction.json \
  --log-file /evidence/terminalworld/value-after-extraction.log
