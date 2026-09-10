#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/seta/venv/bin:$PATH"
export REPRO_AGENT_BUDGET_USD=8
export REPRO_AGENT_MAX_TURNS=100
export PYTHONUNBUFFERED=1
test -f /work/seta/harbor/unix-se-26047/task.toml
git -C /work/seta/upstream apply --check /work/recipes/seta-evol/patches/001-resource-limits.patch
git -C /work/seta/upstream apply /work/recipes/seta-evol/patches/001-resource-limits.patch
cd /work/seta/upstream/datasynth/evol_pipeline
python run_evol_orchestrator.py /work/recipes/seta-evol/config/smoke.yaml evolve --dry-run
timeout --signal=TERM --kill-after=30 2800 python run_evol_orchestrator.py /work/recipes/seta-evol/config/smoke.yaml evolve
