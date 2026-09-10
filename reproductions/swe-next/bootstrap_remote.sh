#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/swe-next /evidence/swe-next
cd /work/swe-next
if [ ! -d upstream/.git ]; then git clone https://github.com/TIGER-AI-Lab/SWE-Next upstream; fi
git -C upstream checkout --detach b55c0841f364f9fe7363b2012cd0ae8d8afdf872
if [ ! -x venv/bin/python ]; then uv venv venv --python 3.12; fi
uv pip install --python venv/bin/python -e ./upstream 'pyarrow<21'
apt-get update -qq
apt-get install -y zsh
uv pip freeze --python venv/bin/python > /evidence/swe-next/requirements.lock.txt
git -C upstream rev-parse HEAD > /evidence/swe-next/source-commit.txt
