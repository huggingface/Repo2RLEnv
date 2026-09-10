#!/usr/bin/env bash
set -euo pipefail
cd /work/r2e/upstream
.venv/bin/python - <<'PY'
import subprocess
from pathlib import Path
from r2e.paths import REPOS_DIR
result = subprocess.run(['.venv/bin/r2e','build','-e','pilot'], input='y\ny\n', text=True, timeout=1200)
path = REPOS_DIR/'r2e_final_dockerfile.dockerfile'
if path.exists():
    Path('/evidence/r2e/native.Dockerfile').write_bytes(path.read_bytes())
result.check_returncode()
subprocess.run(['docker','image','inspect','r2e:pilot'],check=True,stdout=Path('/evidence/r2e/image.json').open('w'))
PY
