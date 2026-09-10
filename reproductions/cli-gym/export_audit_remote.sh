#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/control/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime
python /work/recipes/cli-gym/export_harbor.py
