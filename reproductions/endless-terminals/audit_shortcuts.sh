#!/usr/bin/env bash
set -euo pipefail
# Deliberately invalid candidate solutions. These are rejection probes, never oracles.
export PATH="/work/endless/venv/bin:$PATH"
python - <<'PY'
import json,os,shutil
from pathlib import Path
cases={
 'task_000000_48ab827c':('/work/endless/harbor', '''#!/bin/bash
mkdir -p /home/user/dev_project/venv_proj/bin
cat > /home/user/dev_project/venv_proj/bin/python <<'STUB'
#!/bin/sh
printf 'requests==0.0\nVersion: 0.0\n'
STUB
chmod +x /home/user/dev_project/venv_proj/bin/python
printf 'Virtual Environment Path: /home/user/dev_project/venv_proj\nInstalled Packages:\n- requests==0.0\n' > /home/user/dev_project/venv_setup.log
''','Fake interpreter prints package metadata; no virtual environment or requests installation.'),
 'task_000000_bf61fda1':('/work/endless/harbor-batch-02', r'''#!/bin/bash
cd /home/user/company_data
tar -czf /home/user/archives/backup_archive.tar.gz *.txt
: > /home/user/archives/secure_backup.aes
python3 - <<'SCRIPT'
from pathlib import Path
from datetime import datetime
total=sum(p.stat().st_size for p in Path('/home/user/company_data').glob('*.txt'))
compressed=Path('/home/user/archives/backup_archive.tar.gz').stat().st_size
Path('/home/user/archive_log.txt').write_text(f'{total} bytes\n{compressed} bytes\nAES-256\n'+datetime.now().strftime('%Y-%m-%d %H:%M:%S')+'\n')
SCRIPT
''','Zero-byte encrypted archive; encryption never performed.'),
 'task_000000_eb94a8e1':('/work/endless/harbor-batch-02', '''#!/bin/bash
mkdir -p /home/user/data /home/user/logs
printf 'id,date,price,quantity,category\n' > /home/user/data/cleaned_data.csv
cat > /home/user/logs/cleaning_log.txt <<'LOG'
Total number of rows before cleaning: 0
Total number of rows after cleaning: 0
Number of rows removed due to missing critical data: 0
Confirmation of the data type standardization for the 'date' and 'category' columns
LOG
''','Deletes all data and fabricates row counts; no cleaning performed.')
}
receipts=[]
for task,(source,attack,reason) in cases.items():
    if os.environ.get('REPRO_ATTACK_TASK') and os.environ['REPRO_ATTACK_TASK']!=task:
        continue
    target=Path('/work/endless')/os.environ.get('REPRO_ATTACK_RUN','adversarial-01')/task
    shutil.copytree(Path(source)/task,target)
    solution=target/'solution'
    if solution.exists():
        shutil.rmtree(solution) # Only the new disposable copy.
    solution.mkdir()
    (solution/'solve.sh').write_text(attack)
    receipts.append({'task':task,'purpose':'adversarial rejection probe','invalid_behavior':reason})
Path('/evidence/endless/'+os.environ.get('REPRO_ATTACK_RUN','adversarial-01')+'-cases.json').write_text(json.dumps(receipts,indent=2)+'\n')
PY
for task in "/work/endless/${REPRO_ATTACK_RUN:-adversarial-01}"/task_*; do
    python /work/recipes/runtime/harbor_artifacts.py audit "$task" "/evidence/endless/${REPRO_ATTACK_RUN:-adversarial-01}/$(basename "$task")"
done
