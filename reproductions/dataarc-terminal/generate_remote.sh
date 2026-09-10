#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH=/work/dataarc-terminal/upstream:/work/recipes/runtime
export API_KEY="$OPENAI_API_KEY"
export REPRO_API_RESPONSES=/evidence/dataarc-terminal/generation-api.jsonl
test ! -d /work/dataarc-terminal/native-output
/work/dataarc-terminal/venv/bin/python /work/recipes/dataarc-terminal/native_driver.py /work/recipes/dataarc-terminal/pilot.yaml
/work/dataarc-terminal/venv/bin/python - <<'PY'
from pathlib import Path
import json
tasks = list(Path('/work/dataarc-terminal/native-output/tasks').glob('*/task.toml'))
print(json.dumps({'native_emitted':len(tasks), 'validation':'static only; execution pending'}))
assert len(tasks) == 1, 'No native materialization; inspect raw response and schema errors'
PY
