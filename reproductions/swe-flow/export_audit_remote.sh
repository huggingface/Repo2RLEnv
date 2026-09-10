#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/control/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime:/work/swe-flow/upstream
uv pip install --python /work/control/venv/bin/python networkx gitpython
python /work/recipes/swe-flow/export_harbor.py
