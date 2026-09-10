#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/swesmith/venv/bin:$PATH"
export PYTHONUNBUFFERED=1
cd /work/swesmith/upstream
python -m swesmith.bug_gen.procedural.generate mewwts__addict.75284f95 --seed 24 --interleave --max_bugs 5 --max_entities 50 --max_candidates 30 --timeout_seconds 300
python -m swesmith.bug_gen.collect_patches logs/bug_gen/mewwts__addict.75284f95 --num_bugs 10
timeout --signal=TERM --kill-after=30 1800 python -m swesmith.harness.valid logs/bug_gen/mewwts__addict.75284f95_all_patches_n10.json --workers 1
python /work/recipes/swe-smith/validated_instances.py
