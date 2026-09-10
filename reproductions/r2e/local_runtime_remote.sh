#!/usr/bin/env bash
set -euo pipefail
# R2E's documented --local mode runs inside the remote Modal VM, never on the Mac.
cd /work/r2e/upstream
uv pip install --python .venv/bin/python -e /root/buckets/local_repoeval_bucket/repos/google-research___python-graphs
.venv/bin/python - <<'PY'
import subprocess, json
from pathlib import Path
from r2e.paths import REPOS_DIR, EXTRACTED_DATA_DIR
from r2e.utils.data import load_functions
import python_graphs, r2e_test_server
repo = REPOS_DIR/'google-research___python-graphs'
functions = load_functions(EXTRACTED_DATA_DIR/'pilot_extracted.json')
rows = [{'name':f.name,'file':str(f.file.file_path),'code':f.code} for f in functions[:15]]
Path('/evidence/r2e/local-runtime.json').write_text(json.dumps({
    'execution_host':'Remote Modal VM', 'native_mode':'--local',
    'reason':'Original Docker builder failed at removed Ubuntu packages; native supported local mode preserves the generator and execution service',
    'repo_commit':subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip(),
    'candidate_functions':rows,
},indent=2))
PY
uv pip freeze --python .venv/bin/python > /evidence/r2e/local-requirements.lock.txt
