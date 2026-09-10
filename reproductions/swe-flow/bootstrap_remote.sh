#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/swe-flow /evidence/swe-flow
cd /work/swe-flow
if [ ! -d upstream/.git ]; then git clone https://github.com/Hambaobao/SWE-Flow upstream; fi
git -C upstream checkout --detach 7da5b046fa1dc184674e4e94a9989be56c39e4e7
if [ ! -d trace/.git ]; then git clone https://github.com/Hambaobao/SWE-Flow-Trace trace; fi
git -C trace checkout --detach e7251448ae7d29c7de5fdcda2a3bc175e547420e
if [ ! -x /work/swe-flow/venv/bin/python ]; then uv venv /work/swe-flow/venv --python 3.12; fi
uv pip install --python /work/swe-flow/venv/bin/python -e /work/swe-flow/upstream
docker pull hambaobao/sweflow:0b01001001__--__spectree
docker image inspect hambaobao/sweflow:0b01001001__--__spectree > /evidence/swe-flow/image.json
uv pip freeze --python /work/swe-flow/venv/bin/python > /evidence/swe-flow/requirements.lock.txt
