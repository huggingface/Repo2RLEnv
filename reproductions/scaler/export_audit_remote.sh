#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/control/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime
uv pip install --python /work/control/venv/bin/python math-verify==0.9.0
python /work/recipes/scaler/export_harbor.py
