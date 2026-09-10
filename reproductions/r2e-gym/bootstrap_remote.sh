#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/r2e-gym /evidence/r2e-gym
cd /work/r2e-gym
if [ ! -d upstream/.git ]; then git clone https://github.com/R2E-Gym/R2E-Gym upstream; fi
git -C upstream checkout --detach 0d94c4eb9431cd195c55a7ea3abd54006c9a1735
cd upstream
uv sync --frozen --python 3.12
uv pip install --python .venv/bin/python -e /work/r2e/upstream 'pyarrow<21'
uv pip freeze --python .venv/bin/python > /evidence/r2e-gym/requirements.lock.txt
# Native scanner has no commit-count limit. A shallow 100-commit input bounds
# mining while retaining the original filtering and generation implementation.
if [ ! -d pyramid/.git ]; then git clone --depth 100 https://github.com/Pylons/pyramid pyramid; fi
git -C pyramid rev-parse HEAD > /evidence/r2e-gym/input-commit.txt
PYTHONPATH=src .venv/bin/python -c 'from r2egym.repo_analysis.repo_analysis_args import RepoAnalysisArgs; print(RepoAnalysisArgs(repo_name="pyramid",n_cpus=1))'
