#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/tmax/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime:/work/tmax/upstream
export PYTHONUNBUFFERED=1
cd /work/tmax/upstream
python - <<'PY'
from pathlib import Path
import shutil,json
name='task_000000_917b92fc'
original=Path('/work/tmax/native-smoke-01')/name
target=Path('/work/tmax/runtime-fixed')/name
shutil.copytree(original,target,ignore=shutil.ignore_patterns('*.sif'))
p=target/'container.def'; lines=p.read_text().splitlines()
post=next(i for i,line in enumerate(lines) if line.strip()=='%post')
end=next((i for i in range(post+1,len(lines)) if lines[i].lstrip().startswith('%')),len(lines))
lines.insert(end,'    python3 -m pip install pytest-timeout==2.4.0\n')
p.write_text('\n'.join(lines)+'\n')
export=Path('/work/tmax/harbor-runtime-fixed')/name
shutil.copytree(Path('/work/tmax/harbor-v2')/name,export)
dockerfile=export/'environment/Dockerfile'
dockerfile.write_text(dockerfile.read_text()+'\nRUN python3 -m pip install pytest-timeout==2.4.0\n')
Path('/work/tmax/runtime-fixed/deviations.json').write_text(json.dumps({
 'task':name,'change':'Install pytest-timeout==2.4.0 in both native and Docker images',
 'reason':'Hidden final verifier invokes --timeout=120, but the generated environment omits its pytest plugin; otherwise completed solution gets 50/51.',
 'instruction_and_tests':'unchanged','native_baseline':'preserved separately'},indent=2))
PY
task=/work/tmax/runtime-fixed/task_000000_917b92fc
apptainer build "$task/container.sif" "$task/container.def" > /evidence/tmax/runtime-fixed-build.log 2>&1
python /work/recipes/runtime/replay_native_calls.py "$task" /evidence/tmax/probe-02-api.jsonl /evidence/tmax/recovered-replay-02 /work/tmax/harbor-runtime-fixed/task_000000_917b92fc
python /work/recipes/runtime/harbor_artifacts.py audit /work/tmax/harbor-runtime-fixed/task_000000_917b92fc /evidence/tmax/runtime-fixed-harbor-01
