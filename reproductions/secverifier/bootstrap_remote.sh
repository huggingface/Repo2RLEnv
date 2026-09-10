#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/secverifier /evidence/secverifier
cd /work/secverifier
if [ ! -d upstream/.git ]; then git clone https://github.com/SEC-bench/SecVerifier upstream; fi
git -C upstream checkout --detach 93abf5900327809eacff66fec40d45eb95221acb
cd upstream
if [ ! -x .venv/bin/python ]; then uv venv .venv --python 3.12; fi
export VIRTUAL_ENV=/work/secverifier/upstream/.venv
export CC=gcc CXX=g++
uvx --from poetry==2.1.4 poetry install --only main --no-interaction
uv pip install --python .venv/bin/python pandas datasets
uv pip freeze --python .venv/bin/python > /evidence/secverifier/requirements.lock.txt
.venv/bin/python multi-agent.py --help > /evidence/secverifier/native-help.txt
