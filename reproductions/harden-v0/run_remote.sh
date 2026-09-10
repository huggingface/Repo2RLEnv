#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/harden-v0/venv/bin:$PATH"
export PYTHONPATH=/work/harden-v0/upstream
cd /work/harden-v0/upstream
python -m harden --task-id task_000000_48ab827c --tasks-dir /work/harden-v0/input \
  --output-dir /work/harden-v0/native-output --oracle \
  --hacker-model anthropic/claude-sonnet-4-6 --fixer-model anthropic/claude-sonnet-4-6 \
  --max-iterations 1 --hacker-retries 1 --hacker-max-turns 12 --fixer-max-turns 24 \
  --reasoning-effort none --max-tokens 4096 --hacker-privileged \
  --summary-model '' --replay-enabled --replay-retries 1 \
  --hacker-timeout-multiplier 1 --fixer-timeout-multiplier 1 \
  --harbor-config /work/recipes/harden-v0/pilot.yaml
