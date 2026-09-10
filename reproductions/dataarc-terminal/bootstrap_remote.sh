#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/dataarc-terminal /evidence/dataarc-terminal
cd /work/dataarc-terminal
if [ ! -d upstream/.git ]; then git clone --filter=blob:none https://github.com/DataArcTech/DataArc-SynData-Toolkit upstream; fi
git -C upstream checkout --detach 2a1d65ec8dcfaea2458d67e1fb18078cce6420b9
if [ ! -x venv/bin/python ]; then uv venv venv --python 3.11.13; fi
# Run the released terminal synthesis entry point, without unrelated GPU training
# packages. Keep this dependency subset as an explicit installation deviation.
uv pip install --python venv/bin/python openai pydantic pyyaml litellm
uv pip freeze --python venv/bin/python > /evidence/dataarc-terminal/requirements.lock.txt
PYTHONPATH=/work/dataarc-terminal/upstream venv/bin/python -c 'from sdgsystem.agentic_data.terminal_bench import TerminalBenchSynthesisConfig'
