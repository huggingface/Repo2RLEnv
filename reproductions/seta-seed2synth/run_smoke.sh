#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/seta/venv/bin:$PATH"
export REPRO_AGENT_BUDGET_USD=8
export REPRO_AGENT_MAX_TURNS=100
export PYTHONUNBUFFERED=1
git -C /work/seta/upstream apply --check /work/recipes/seta-seed2synth/patches/001-resource-limits.patch
git -C /work/seta/upstream apply /work/recipes/seta-seed2synth/patches/001-resource-limits.patch
cd /work/seta/upstream/datasynth/seed2synth_pipeline
python run_orchestrator.py /work/recipes/seta-seed2synth/config/smoke.yaml --dry-run
timeout --signal=TERM --kill-after=30 2700 python run_orchestrator.py /work/recipes/seta-seed2synth/config/smoke.yaml --synth-only
