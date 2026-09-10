#!/usr/bin/env bash
set -euo pipefail
export REPRO_INPUT_REPO=Pylons/pyramid REPRO_MAX_COMMITS=30
export REPRO_API_RESPONSES=/evidence/swe-next/pyramid-openai-api.jsonl
bash /work/recipes/swe-next/generate_remote.sh
