#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/swesmith/venv/bin:$PATH"
python /work/recipes/runtime/harbor_modal_network_compat.py > /evidence/swesmith/network-compat.json
for task in /work/swesmith/harbor/*/task.toml; do
    directory=$(dirname "$task")
    python /work/recipes/runtime/harbor_artifacts.py audit "$directory" "/evidence/swesmith/harbor-01/$(basename "$directory")"
done
