#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/tmax /evidence/tmax
git clone --filter=blob:none --no-checkout https://github.com/hamishivi/tmax.git /work/tmax/upstream
git -C /work/tmax/upstream sparse-checkout set rl_data/generator rl_data/containers rl_data/scripts Vanillux2Agent
git -C /work/tmax/upstream checkout --detach 7387d2f9142397a458dc39f0827a2ab0b4c03cda
uv venv /work/tmax/venv --python 3.12
uv pip install --python /work/tmax/venv/bin/python -r /work/recipes/tmax/config/requirements.lock.txt
uv pip freeze --python /work/tmax/venv/bin/python > /evidence/tmax/requirements.lock.txt
git -C /work/tmax/upstream apply /work/recipes/tmax/patches/001-api-receipts.patch
