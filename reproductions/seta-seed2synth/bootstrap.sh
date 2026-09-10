#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/seta /evidence/seta
if [ ! -d /work/seta/upstream ]; then
  git clone --filter=blob:none --no-checkout https://github.com/camel-ai/seta.git /work/seta/upstream
  git -C /work/seta/upstream sparse-checkout set datasynth
  git -C /work/seta/upstream checkout --detach e4715b01174e6c9503fc46120d81dd692ced75e6
fi
uv venv /work/seta/venv --python 3.12
uv pip install --python /work/seta/venv/bin/python -r /work/recipes/seta-seed2synth/config/requirements.lock.txt
uv pip freeze --python /work/seta/venv/bin/python > /evidence/seta/requirements.lock.txt
/work/seta/venv/bin/python - <<'PY'
from claude_agent_sdk import ClaudeAgentOptions
assert 'max_budget_usd' in ClaudeAgentOptions.__dataclass_fields__
assert 'max_turns' in ClaudeAgentOptions.__dataclass_fields__
print('Claude SDK resource options verified')
PY
/work/seta/venv/bin/harbor --version
/work/seta/venv/bin/python /work/recipes/seta-seed2synth/prepare_seeds.py
