#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/scaler /evidence/scaler
cd /work/scaler
if [ ! -d upstream/.git ]; then GIT_LFS_SKIP_SMUDGE=1 git clone --filter=blob:none https://github.com/ALEX-nlp/SCALER upstream; fi
git -C upstream checkout --detach 60c6c5037866c718f4c001ea338f9c5a91cb01ae
if [ ! -x venv/bin/python ]; then uv venv venv --python 3.12; fi
uv pip install --python venv/bin/python datasets openai requests tqdm litellm
uv pip freeze --python venv/bin/python > /evidence/scaler/requirements.lock.txt
docker pull volcengine/sandbox-fusion:server-20250609
docker image inspect volcengine/sandbox-fusion:server-20250609 > /evidence/scaler/sandbox-image.json
docker run -d --name reproduction-sandboxfusion --cpus 2 --memory 4g -p 127.0.0.1:8080:8080 volcengine/sandbox-fusion:server-20250609
venv/bin/python - <<'PY'
import requests,time,json
from pathlib import Path
for attempt in range(30):
    try:
        response=requests.post('http://127.0.0.1:8080/run_code',json={'language':'python','code':'print(6 * 7)'},timeout=5)
        result=response.json()
        if result.get('status')=='Success':
            assert result['run_result']['stdout'].strip()=='42'
            Path('/evidence/scaler/sandbox-smoke.json').write_text(json.dumps(result,indent=2))
            break
    except (requests.RequestException, ValueError):
        pass
    time.sleep(2)
else:
    raise RuntimeError('Published SandboxFusion service did not pass the bounded smoke')
PY
