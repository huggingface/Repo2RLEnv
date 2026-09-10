#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/control /evidence/control
if [ ! -x /work/control/venv/bin/python ]; then uv venv /work/control/venv --python 3.12; fi
uv pip install --python /work/control/venv/bin/python -r /work/recipes/runtime/requirements.txt
export PATH="/work/control/venv/bin:$PATH"
bash /work/recipes/runtime/docker_smoke.sh
python /work/recipes/runtime/harbor_modal_network_compat.py > /evidence/control/network-compat.json
