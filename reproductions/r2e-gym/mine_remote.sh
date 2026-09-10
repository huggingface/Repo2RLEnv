#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/r2e-gym/upstream/.venv/bin:$PATH"
cd /work/r2e-gym/upstream
export PYTHONPATH=/work/r2e-gym/upstream/src:/work/r2e/upstream/src
python - <<'PY'
from pathlib import Path
# The original collector hardcodes its second pool to 32 workers. Preserve work
# and parsers while limiting both pools to the pilot's single worker allowance.
path=Path('src/r2egym/repo_analysis/store_repo_commits.py')
text=path.read_text()
assert 'with Pool(processes=32)' in text or 'with Pool(processes=1)' in text
path.write_text(text.replace('with Pool(processes=32)','with Pool(processes=1)'))
PY
python src/r2egym/repo_analysis/store_repo_commits.py --repo_name pyramid --n_cpus 1
python src/r2egym/repo_analysis/analyze_testable_commits.py --repo_name pyramid \
 --use_local_commit_data --n_cpus 1 --N 500 --keep_only_bug_edit_commits \
 --keep_only_testmatch_commits --keep_only_test_entity_edit_commits
python - <<'PY'
import json
from pathlib import Path
Path('/evidence/r2e-gym/mining-summary.json').write_text(json.dumps({'input':'Pylons/pyramid','history_depth':100,'commit_records':len(list(Path('commit_data/pyramid').glob('*.json')))},indent=2))
PY
