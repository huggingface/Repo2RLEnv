#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/terminalworld /evidence/terminalworld
cd /work/terminalworld
if [ ! -d upstream/.git ]; then git clone https://github.com/EuniAI/TerminalWorld upstream; fi
git -C upstream checkout --detach 784698ba93735470ce1664bff2ec44bcd7b28e15
if [ ! -x venv/bin/python ]; then uv venv venv --python 3.12; fi
uv pip install --python venv/bin/python requests beautifulsoup4 litellm claude-agent-sdk pyyaml 'harbor==0.20.0'
uv pip freeze --python venv/bin/python > /evidence/terminalworld/requirements.lock.txt
export PATH="/work/terminalworld/venv/bin:$PATH"
export PYTHONPATH=/work/terminalworld/upstream
python /work/recipes/runtime/harbor_modal_network_compat.py > /evidence/terminalworld/network-compat.json
python /work/recipes/terminalworld/retrieve_native.py
