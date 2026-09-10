#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/swe-flow/native-output
docker run --name reproduction-sweflow-trace \
  --cpus 2 --memory 4g \
  -v /work/swe-flow/trace:/tmp/SWE-Flow-Trace:ro \
  -v /work/swe-flow/native-output:/tmp/outputs \
  --entrypoint /bin/bash hambaobao/sweflow:0b01001001__--__spectree -lc '
set -euo pipefail
mkdir -p /workspace
cp -a /sweflow/sweflow-build/workspace.backup/. /workspace/
cp -a /tmp/SWE-Flow-Trace /tmp/trace-install
python -m pip install -e /tmp/trace-install
python -m pip freeze > /tmp/outputs/trace-requirements.lock.txt
sweflow-trace-python --project-root /workspace --max-workers 2 --max-tests 5 --random True --random-seed 42 --output-dir /tmp/outputs
'
docker cp reproduction-sweflow-trace:/workspace /work/swe-flow/workspace
/work/swe-flow/venv/bin/sweflow-schedule-python --trace-file /work/swe-flow/native-output/traces.json --output-dir /work/swe-flow/native-output
/work/swe-flow/venv/bin/python - <<'PY'
from pathlib import Path
import json
root = Path('/work/swe-flow/native-output')
traces = json.loads((root/'traces.json').read_text())
schedule = json.loads((root/'development-schedule.json').read_text())
assert traces and schedule
Path('/evidence/swe-flow/trace-summary.json').write_text(json.dumps({'traces':len(traces), 'schedule_steps':len(schedule), 'max_tests':5, 'seed':42}, indent=2))
PY
