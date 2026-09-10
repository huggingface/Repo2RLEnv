#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/swesmith/venv/bin:$PATH"
python - <<'PY'
from pathlib import Path
import shutil,json
source=sorted(Path('/work/swesmith/harbor').glob('*/task.toml'))[0].parent
target=Path('/work/swesmith/blind-audit')/source.name
shutil.copytree(source,target)
p=target/'environment/Dockerfile'
p.write_text(p.read_text()+'\nRUN apt-get update && apt-get install -y tmux && rm -rf /var/lib/apt/lists/*\n')
(target.parent/'deviations.json').write_text(json.dumps({'source':str(source),'addition':'tmux for Harbor external Terminus2 agent; task/tests unchanged','model':'anthropic/claude-sonnet-4-6'},indent=2))
PY
task=$(find /work/swesmith/blind-audit -name task.toml)
directory=$(dirname "$task")
python /work/recipes/runtime/harbor_artifacts.py audit "$directory" /evidence/swesmith/blind-contrast-01
python - <<'PY'
import json
assert json.load(open('/evidence/swesmith/blind-contrast-01/audit.json'))['execution_contrast_passed']
PY
timeout --signal=TERM --kill-after=30 900 harbor run -p "$directory" -a terminus-2 -m anthropic/claude-sonnet-4-6 -e docker -n 1 --max-retries 0 --jobs-dir /evidence/swesmith --job-name blind-sonnet-01 --ak max_turns=20 --ak record_terminal_session=false --ak 'llm_call_kwargs={"max_tokens":4096}'
