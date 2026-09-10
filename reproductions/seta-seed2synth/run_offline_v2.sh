#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/seta/venv/bin:$PATH"
python /work/recipes/runtime/harbor_modal_network_compat.py > /evidence/seta/network-compat-v2.json
python - <<'PY'
from pathlib import Path
import shutil,json
source=Path('/work/seta/harbor/unix-se-26047')
target=Path('/work/seta/offline-v2/unix-se-26047')
shutil.copytree(source,target)
config=target/'task.toml'
config.write_text(config.read_text().replace('[environment]\n','[environment]\nallow_internet = false\n'))
p=target/'environment/Dockerfile'
p.write_text(p.read_text()+'\nRUN /root/.local/bin/uvx -p 3.13 -w pytest==8.4.1 -w pytest-json-ctrf==0.3.5 pytest --version\n'+'\nENV PATH="/root/.local/bin:${PATH}"\nENV UV_OFFLINE=1\n')
(target.parent/'deviations.json').write_text(json.dumps({
 'source':str(source),'changes':['Disable task internet','Pre-cache exact upstream verifier dependencies','Add installed uv to PATH so upstream installer guard works','UV_OFFLINE=1 uses the prebuilt verifier cache'],
 'tests':'byte-identical to native task','instruction':'byte-identical to native task'
},indent=2))
PY
python /work/recipes/runtime/harbor_artifacts.py audit /work/seta/offline-v2/unix-se-26047 /evidence/seta/offline-contrast-03
python - <<'PY'
import json
assert json.load(open('/evidence/seta/offline-contrast-03/audit.json'))['execution_contrast_passed']
PY
timeout --signal=TERM --kill-after=30 900 harbor run -p /work/seta/offline-v2/unix-se-26047 -a terminus-2 -m anthropic/claude-sonnet-4-6 -e docker -n 1 --max-retries 0 --jobs-dir /evidence/seta --job-name blind-sonnet-03 --ak max_turns=20 --ak record_terminal_session=false --ak 'llm_call_kwargs={"max_tokens":4096}'
