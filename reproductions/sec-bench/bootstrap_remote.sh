#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/sec-bench /evidence/sec-bench
cd /work/sec-bench
if [ ! -d upstream/.git ]; then git clone https://github.com/SEC-bench/SEC-bench upstream; fi
git -C upstream checkout --detach 31eb43485a3de47da260be0f978528b1f2314415
if [ ! -x venv/bin/python ]; then uv venv venv --python 3.12; fi
# Image construction uses this subset; the unrelated agent frontend is isolated
# in the separately pinned SecVerifier environment.
uv pip install --python venv/bin/python datasets requests loguru rich jinja2 docker
uv pip freeze --python venv/bin/python > /evidence/sec-bench/requirements.lock.txt
venv/bin/python - <<'PY'
import json, hashlib
from pathlib import Path
from datasets import load_dataset
from huggingface_hub import HfApi
info=HfApi().dataset_info('SEC-bench/Seed')
dataset=load_dataset('SEC-bench/Seed',split='cve',revision=info.sha)
rows=[row for row in dataset if row['instance_id']=='njs.cve-2022-32414']
assert len(rows)==1
Path('/work/sec-bench/seed.jsonl').write_text(json.dumps(rows[0],default=str)+'\n')
Path('/evidence/sec-bench/input.json').write_text(json.dumps({'dataset':'SEC-bench/Seed','revision':info.sha,'split':'cve','instance_id':rows[0]['instance_id'],'fields':list(rows[0]),'input_sha256':hashlib.sha256(Path('/work/sec-bench/seed.jsonl').read_bytes()).hexdigest()},indent=2))
print(json.dumps({'instance':rows[0]['instance_id'],'fields':list(rows[0])}))
PY
