#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/scaler/venv/bin:$PATH"
export PYTHONPATH=/work/scaler/upstream/SCALER:/work/recipes/runtime
uv pip install --python /work/scaler/venv/bin/python math-verify
python /work/recipes/scaler/expand_native.py
