#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/swesmith/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/runtime
export REPRO_API_RESPONSES=/evidence/swesmith/issues-01-api.jsonl
cd /work/swesmith/upstream
git apply --check /work/recipes/swe-smith/patches/001-issue-resource-limit.patch
git apply /work/recipes/swe-smith/patches/001-issue-resource-limit.patch
timeout --signal=TERM --kill-after=30 1200 python -m swesmith.issue_gen.generate --dataset_path /work/swesmith/validated.json --config_file /work/recipes/swe-smith/config/issues.yaml --workers 1
python /work/recipes/swe-smith/export_harbor.py /work/swesmith/validated__ig_llm.json /work/swesmith/harbor
