#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/terminalworld/venv/bin:$PATH"
export PYTHONPATH=/work/terminalworld/upstream:/work/terminalworld/upstream/data_filtering:/work/recipes/runtime
export REPRO_API_RESPONSES=/evidence/terminalworld/filter-api.jsonl
uv pip install --python /work/terminalworld/venv/bin/python json-repair==0.54.2
python - <<'PY'
from pathlib import Path
import json,shutil
from data_filtering.classify_tui import classify_recording
from data_filtering.detect_external_urls import analyze_recording
records = json.loads(Path('/evidence/terminalworld/retrieval.json').read_text())
results = []
for record in records:
    if (record.get('native_pii_filter') or {}).get('label') != 'CLEAN':
        continue
    source = Path('/work/terminalworld/recordings')/record['id']
    _, tui = classify_recording(source)
    urls = analyze_recording(source/'info.json',check_accessibility=True,timeout=15)
    results.append({'id':record['id'],'tui':tui,'urls':urls})
    if tui == 'CLI_ONLY':
        shutil.copytree(source,Path('/work/terminalworld/clean-recordings')/record['id'],dirs_exist_ok=True)
Path('/evidence/terminalworld/input-filters.json').write_text(json.dumps(results,indent=2))
assert Path('/work/terminalworld/clean-recordings/100135').is_dir()
PY
python /work/recipes/terminalworld/native_driver.py data_filtering.score_value \
  --recordings-dir /work/terminalworld/clean-recordings --limit 1 \
  --output /evidence/terminalworld/value-scores.json --log-file /evidence/terminalworld/value-score.log
