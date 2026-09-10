#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/swe-flow/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime
export REPRO_API_RESPONSES=/evidence/swe-flow/synthesis-api.jsonl
uv pip install --python /work/swe-flow/venv/bin/python litellm
python /work/recipes/swe-flow/native_driver.py docstring
python /work/recipes/swe-flow/native_driver.py specification
git config --global user.name 'Reproduction Worker'
git config --global user.email 'reproduction@localhost'
sweflow-create-codebase --project-root /work/swe-flow/workspace \
  --development-schedule /work/swe-flow/native-output/development-schedule.json \
  --docstrings /work/swe-flow/native-output/docstrings.json \
  --temp-dir /work/swe-flow/tmp \
  --output-codebase-dir /work/swe-flow/native-output \
  --output-dir /work/swe-flow/native-output
python -m sweflow.utils.merge --repository 0b01001001/spectree --input-dir /work/swe-flow/native-output --output-file /work/swe-flow/native-output/dataset.jsonl
python - <<'PY'
import json
from pathlib import Path
rows = [json.loads(line) for line in Path('/work/swe-flow/native-output/dataset.jsonl').read_text().splitlines()]
assert rows and all(row['patch'] and row['problem_statement'] for row in rows)
Path('/evidence/swe-flow/synthesis-summary.json').write_text(json.dumps({'native_emitted':len(rows), 'execution_contrast':'pending'}, indent=2))
PY
