#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/swe-gen /evidence/swe-gen
cd /work/swe-gen
if [ ! -d upstream/.git ]; then git clone https://github.com/abundant-ai/SWE-gen upstream; fi
git -C upstream checkout --detach 14e185f413f7bff03f8f9fec6fb246681bf61d74
if [ ! -x venv/bin/python ]; then uv venv venv --python 3.12; fi
uv pip install --python venv/bin/python -e upstream 'harbor==0.20.0' litellm
export PATH="/work/swe-gen/venv/bin:$PATH"
python /work/recipes/runtime/harbor_modal_network_compat.py > /evidence/swe-gen/network-compat.json
swegen create --help > /evidence/swe-gen/create-help.txt
uv pip freeze --python venv/bin/python > /evidence/swe-gen/requirements.lock.txt
