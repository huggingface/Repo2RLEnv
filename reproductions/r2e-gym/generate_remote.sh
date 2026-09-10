#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/r2e-gym/upstream/.venv/bin:$PATH"
export OPENAI_KEY="$OPENAI_API_KEY"
export PYTHONPATH=/work/recipes/r2e-gym/telemetry:/work/recipes/runtime:/work/r2e-gym/upstream/src:/work/r2e/upstream/src
export REPRO_API_RESPONSES=/evidence/r2e-gym/generation-api.jsonl
cd /work/r2e-gym/upstream
# The released Dockerfile lookup retains a pre-rename r2e_edits directory.
if [ ! -e src/r2e_edits ]; then ln -s r2egym src/r2e_edits; fi
python src/r2egym/repo_analysis/repo_testextract.py --repo_name pyramid \
 --use_local_commit_data --n_cpus 1 --n_cpus_docker 1 --N 500 \
 --keep_only_bug_edit_commits --keep_only_testmatch_commits \
 --keep_only_test_entity_edit_commits --model_name gpt-4o --max_tokens 4096 \
 --build_dockers True --push_dockers False --cleanup_dockers False
python - <<'PY'
import json
from pathlib import Path
from r2e.paths import REPOS_DIR
records=[]
for p in REPOS_DIR.glob('pyramid_*/execution_result.json'):
    data=json.loads(p.read_text());records.append({'path':str(p),'result':data})
Path('/evidence/r2e-gym/generation-summary.json').write_text(json.dumps(records,indent=2))
print(json.dumps({'execution_records':len(records)}))
PY
