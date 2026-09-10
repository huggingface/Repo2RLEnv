#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/seta/venv/bin:$PATH"
export REPRO_AGENT_BUDGET_USD=8
export REPRO_AGENT_MAX_TURNS=100
export PYTHONUNBUFFERED=1
python - <<'PY'
import json, shutil
from pathlib import Path
root=Path('/work/seta/evol-repeat-input')
root.mkdir(exist_ok=True)
selected=[]
excluded=[]
for task in ['unix-se-73498','unix-se-52313']:
    native=json.load(open(f'/work/seta/synth_data/unix_linux_se/{task}/synth_info.json'))
    audit=json.load(open(f'/evidence/seta/harbor-repeat-01/{task}/audit.json'))
    if native['verdict']=='PASS' and audit['execution_contrast_passed']:
        if not (root/task).exists():
            shutil.copytree(Path('/work/seta/harbor')/task, root/task)
        selected.append(task)
    else:
        assert not (root/task).exists(), 'Failed parent must not enter generation'
        excluded.append(task)
assert selected and sorted(p.name for p in root.iterdir())==sorted(selected)
Path('/evidence/seta-evol/repeat-selection.json').write_text(json.dumps({'selected':selected,'excluded':excluded},indent=2)+'\n')
PY
cd /work/seta/upstream/datasynth/evol_pipeline
python run_evol_orchestrator.py /work/recipes/seta-evol/config/repeat.yaml evolve --dry-run
timeout --signal=TERM --kill-after=30 3000 python run_evol_orchestrator.py /work/recipes/seta-evol/config/repeat.yaml evolve
python - <<'PY'
import json,subprocess
from pathlib import Path
for parent in json.load(open('/evidence/seta-evol/repeat-selection.json'))['selected']:
    task=parent+'__b1'
    source=Path('/work/seta/evol_data')/task
    info=json.load(open(source/'synth_info.json'))
    if info.get('verdict')!='PASS':
        continue
    destination=Path('/work/seta/evol-harbor')/task
    subprocess.run(['python','/work/recipes/runtime/harbor_artifacts.py','export',str(source),str(destination)],check=True)
    subprocess.run(['python','/work/recipes/runtime/harbor_artifacts.py','audit',str(destination),'/evidence/seta-evol/harbor-repeat-01/'+task],check=True)
PY
