#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/harden-v0 /evidence/harden-v0
cd /work/harden-v0
if [ ! -d upstream/.git ]; then git clone https://github.com/few-sh/harden-v0 upstream; fi
git -C upstream checkout --detach 342b8474e0c0cf96e4a8313fd2e26c7a11d51193
if [ ! -x venv/bin/python ]; then uv venv venv --python 3.12; fi
uv pip install --python venv/bin/python -r upstream/requirements.txt 'harbor==0.20.0'
export PATH="/work/harden-v0/venv/bin:$PATH"
export PYTHONPATH=/work/harden-v0/upstream
python /work/recipes/runtime/harbor_modal_network_compat.py > /evidence/harden-v0/network-compat.json
python - <<'PY'
from pathlib import Path
import shutil,json
source=Path('/work/previous-pilot/endless-terminals/harbor/task_000000_48ab827c')
target=Path('/work/harden-v0/input')/source.name
shutil.copytree(source,target)
p=target/'environment/Dockerfile'
p.write_text(p.read_text()+'\nRUN apt-get update && apt-get install -y tmux && rm -rf /var/lib/apt/lists/*\n')
Path('/evidence/harden-v0/input.json').write_text(json.dumps({
    'source':str(source),'task':str(target), 'component_not_new_environment':True,
    'compatibility':'tmux added for Harbor Terminus-2; original instruction, verifier and replay reference preserved',
    'known_weakness':'A fake Python executable received reward in the first-pilot independent audit'
},indent=2))
PY
python -m harden --help > /evidence/harden-v0/native-help.txt
uv pip freeze --python venv/bin/python > /evidence/harden-v0/requirements.lock.txt
