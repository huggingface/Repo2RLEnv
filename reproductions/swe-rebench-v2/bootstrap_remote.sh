#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/swe-rebench-v2 /evidence/swe-rebench-v2
cd /work/swe-rebench-v2
if [ ! -d upstream/.git ]; then git clone https://github.com/SWE-rebench/SWE-rebench-V2 upstream; fi
git -C upstream checkout --detach c71902a8cf8d2b725f63d51f199f4d3e56f68d2d
if [ ! -x venv/bin/python ]; then uv venv venv --python 3.12; fi
uv pip install --python venv/bin/python -r upstream/requirements.txt
uv pip freeze --python venv/bin/python > /evidence/swe-rebench-v2/requirements.lock.txt
mkdir -p /work/swe-rebench-v2/native-output
cp upstream/sample.json /work/swe-rebench-v2/native-output/input.json
sha256sum upstream/sample.json > /evidence/swe-rebench-v2/input.sha256
