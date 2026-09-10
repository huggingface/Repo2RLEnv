#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/cli-gym/venv/bin:$PATH"
export PYTHONPATH=/work/cli-gym/upstream/src:/work/recipes/runtime
export LLM_MODEL=openai/gpt-4o
export OPENAI_API_BASE=https://api.openai.com/v1
export REPRO_API_RESPONSES=/evidence/cli-gym/generation-config-api.jsonl
cd /work/cli-gym/upstream
test ! -d CLI-Gym/destruction_tasks-config/addict
python /work/recipes/cli-gym/native_driver.py generate-config
python - <<'PY'
from pathlib import Path
import json
Path('/evidence/cli-gym/candidate-selection.json').write_text(json.dumps({
    'first_candidate': 'i_o_overload_sabotage', 'native_execution': 'not_started',
    'reason': 'Proposal requests 50 concurrent 1 GB writes. Withheld under the pilot resource budget; not counted as an observed native failure.',
    'second_input': 'Tamper with configuration files',
    'selection': 'Documented upstream directions option; original prompts and acceptance unchanged.'
}, indent=2))
PY
