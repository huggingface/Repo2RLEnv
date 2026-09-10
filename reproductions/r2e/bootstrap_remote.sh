#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/r2e /evidence/r2e
cd /work/r2e
if [ ! -d upstream/.git ]; then git clone https://github.com/r2e-project/r2e upstream; fi
git -C upstream checkout --detach bcbed156711bb939de14aa46b27eee15073f5272
cd upstream
uv sync --frozen --python 3.11
.venv/bin/r2e --help > /evidence/r2e/native-help.txt
uv pip freeze --python .venv/bin/python > /evidence/r2e/requirements.lock.txt
.venv/bin/r2e setup -r https://github.com/google-research/python-graphs --cloning_multiprocess 1
.venv/bin/python -c 'import nltk; assert nltk.download("punkt_tab", quiet=True)'
.venv/bin/r2e extract -e pilot --extraction_multiprocess 1 --overwrite_extracted
.venv/bin/python - <<'PY'
from pathlib import Path
from r2e.paths import EXTRACTED_DATA_DIR, REPOS_DIR
import json
path = EXTRACTED_DATA_DIR/'pilot_extracted.json'
records = json.loads(path.read_text())
assert records
Path('/evidence/r2e/extraction.json').write_text(json.dumps({'path':str(path),'functions':len(records),'repos_dir':str(REPOS_DIR)},indent=2))
PY
