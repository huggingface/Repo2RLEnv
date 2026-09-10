#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/terminalworld/venv/bin:$PATH"
export PYTHONPATH=/work/terminalworld/upstream:/work/recipes/runtime
export REPRO_API_RESPONSES=/evidence/terminalworld/tests-api.jsonl
cd /work/terminalworld/upstream
python - <<'PY'
from pathlib import Path
import hashlib, json, shutil
task=Path('/work/terminalworld/native-tasks/100135')
# Original post-build artifact relocation moves required COPY assets out of
# environment/. Restore the exact generated file, retaining its archived copy.
source=task/'pipeline_artifacts/environment/entrypoint.sh'
destination=task/'environment/entrypoint.sh'
shutil.copy2(source,destination)
Path('/evidence/terminalworld/build-context-compat.json').write_text(json.dumps({
    'source':str(source),'restored':str(destination),
    'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
    'reason':'Native artifact relocation removed a Dockerfile COPY dependency',
},indent=2))
PY
python /work/recipes/terminalworld/native_driver.py test_generation.generate_tests \
 --tasks-dir /work/terminalworld/native-tasks --ids 100135 --workers 1 \
 --execution-timeout 300 --log-file /evidence/terminalworld/tests.log
test -s /work/terminalworld/native-tasks/100135/tests/test_state.py
