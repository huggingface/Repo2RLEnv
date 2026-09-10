#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/swesmith /evidence/swesmith
git clone https://github.com/SWE-bench/SWE-smith.git /work/swesmith/upstream
git -C /work/swesmith/upstream checkout --detach 9b74ac08118a85c39c356802f7961893af73e07f
uv venv /work/swesmith/venv --python 3.12
uv pip install --python /work/swesmith/venv/bin/python -r /work/recipes/swe-smith/config/requirements.lock.txt
uv pip freeze --python /work/swesmith/venv/bin/python > /evidence/swesmith/requirements.lock.txt
cd /work/swesmith/upstream
/work/swesmith/venv/bin/python - <<'PY'
from swesmith.profiles import registry
import json
from pathlib import Path
profile=registry.get('mewwts__addict.75284f95')
record={'repo':profile.repo_name,'image':profile.image_name,'test_cmd':profile.test_cmd,'commit':profile.commit}
Path('/evidence/swesmith/profile.json').write_text(json.dumps(record,indent=2))
print(json.dumps(record,indent=2))
profile.pull_image()
PY
