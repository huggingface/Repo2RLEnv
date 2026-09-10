#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/seta/venv/bin:$PATH"
python - <<'PY'
import hashlib,json,shutil
from pathlib import Path
source=Path('/work/seta/harbor/unix-se-52313')
target=Path('/work/seta/reference-fixed/unix-se-52313')
shutil.copytree(source,target)
solution=target/'solution/solve.sh'
original=solution.read_text()
assert original.count('$(date +%s)')==2
assert original.count('total_elapsed=$(( end_epoch - start_epoch ))')==1
fixed=original.replace('$(date +%s)','$(date +%s%N)').replace('total_elapsed=$(( end_epoch - start_epoch ))', 'total_ns=$(( end_epoch - start_epoch ))\nprintf -v total_elapsed \'%d.%09d\' "$((total_ns / 1000000000))" "$((total_ns % 1000000000))"')
solution.write_text(fixed)
changed=[]
for f in source.rglob('*'):
    if f.is_file() and f.read_bytes()!=(target/f.relative_to(source)).read_bytes():
        changed.append(str(f.relative_to(source)))
assert changed==['solution/solve.sh'],changed
receipt={'variant':'reference precision repair','changed_files':changed,'original_sha256':hashlib.sha256(original.encode()).hexdigest(),'fixed_sha256':hashlib.sha256(fixed.encode()).hexdigest(),'reason':'Whole-second timestamps can report zero elapsed time for a subsecond pipeline; preserve nanosecond precision. Instruction and verifier unchanged.'}
Path('/evidence/seta/reference-precision-repair.json').write_text(json.dumps(receipt,indent=2)+'\n')
PY
for attempt in 1 2 3; do
    python /work/recipes/runtime/harbor_artifacts.py audit /work/seta/reference-fixed/unix-se-52313 "/evidence/seta/reference-fixed-0${attempt}"
done
