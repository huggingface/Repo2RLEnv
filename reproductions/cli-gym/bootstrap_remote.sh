#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/cli-gym /evidence/cli-gym
cd /work/cli-gym
if [ ! -d upstream/.git ]; then git clone https://github.com/LiberCoders/CLI-Gym upstream; fi
git -C upstream checkout --detach 48bb920b728a25a55a5b442303e901919654599e
if [ ! -x /work/cli-gym/venv/bin/python ]; then uv venv /work/cli-gym/venv --python 3.12; fi
uv pip install --python /work/cli-gym/venv/bin/python -e /work/cli-gym/upstream
export PATH="/work/cli-gym/venv/bin:$PATH"
export PYTHONPATH=/work/cli-gym/upstream/src
cd upstream
test ! -f config.toml
python - <<'PY'
import json
from pathlib import Path
from cli_gym.build_destruction_task.extract_uts import extract_uts
source = Path('/work/previous-pilot/swe-smith/validated.json')
rows = json.loads(source.read_text())
assert isinstance(rows, list) and len(rows) == 5
data = Path('/work/cli-gym/input'); data.mkdir(exist_ok=True)
(data/'train.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
image = 'swebench/swesmith.x86_64.mewwts_1776_addict.75284f95'
repo, output = extract_uts(image, dataset_path=str(data))
assert repo == 'addict' and json.loads(Path(output).read_text())
Path('/evidence/cli-gym/input.json').write_text(json.dumps({
    'source': 'first-pilot SWE-smith native validated rows', 'rows': len(rows),
    'healthy_image': image, 'test_extractor': 'upstream extract_uts'}, indent=2))
PY
cg pull swebench/swesmith.x86_64.mewwts_1776_addict.75284f95@sha256:f8650aba9f52e514b3adf48c0c1855285179754466d742785c21f69da2da0a8e terminus-2
docker image inspect cli-gym-addict-terminus-2:latest > /evidence/cli-gym/image.json
uv pip freeze --python /work/cli-gym/venv/bin/python > /evidence/cli-gym/requirements.lock.txt
