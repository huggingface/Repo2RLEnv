#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/endless /evidence/endless
git clone https://github.com/kanishkg/endless-terminals.git /work/endless/upstream
git -C /work/endless/upstream checkout --detach 99f4c74b75faacf21e53d3dc01df170902e924cb
uv venv /work/endless/venv --python 3.12
uv pip install --python /work/endless/venv/bin/python -e '/work/endless/upstream[harbor]' 'harbor==0.20.0' anthropic
uv pip freeze --python /work/endless/venv/bin/python > /evidence/endless/requirements.lock.txt
cp /work/apptainer/ubuntu_22.04.sif /work/endless/upstream/ubuntu_22.04.sif
git -C /work/endless/upstream apply /work/recipes/endless-terminals/patches/001-api-endpoint-and-receipts.patch
