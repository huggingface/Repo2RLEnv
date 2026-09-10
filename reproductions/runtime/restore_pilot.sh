#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/control/venv/bin:$PATH"
python - <<'PY'
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from sys import path
path.insert(0, '/work/recipes/runtime')
from harbor_artifacts import hashes

destination = Path('/work/previous-pilot')
with tempfile.TemporaryDirectory(prefix='reproduction-restore-') as temporary:
    restored = Path(temporary) / 'verified'
    subprocess.run([sys.executable, '/work/recipes/runtime/fetch_artifacts.py',
                    '/work/recipes/artifacts.json', str(restored)], check=True)
    if destination.exists():
        if hashes(destination) != hashes(restored):
            raise ValueError('Existing pilot artifacts differ from the immutable archive')
    else:
        shutil.copytree(restored, destination)
receipt = json.loads(Path('/work/recipes/artifacts.json').read_text())
Path('/evidence/control/restored-pilot.json').write_text(json.dumps(receipt, indent=2))
print(json.dumps({'previous_pilot': 'verified against the pinned archive'}))
PY
