#!/bin/bash
# scripts/add_to_collection.sh
#
# Add a published HF dataset to the Repo2RLEnv collection.
#
# Usage:
#   ./scripts/add_to_collection.sh AdithyaSK/repo2rlenv-equivalence-tests "Optional note"
#
# The collection ID is hard-coded to
#   AdithyaSK/repo2rlenv-verifiable-rl-environments-6a15e7eee7c112fe841b2990
# (change here or via COLLECTION_SLUG env var).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [ -f .env ]; then
    set -a; source ./.env; set +a
fi

DATASET_ID="${1:?usage: $0 <owner/dataset> [\"note\"]}"
NOTE="${2:-}"
COLLECTION_SLUG="${COLLECTION_SLUG:-AdithyaSK/repo2rlenv-verifiable-rl-environments-6a15e7eee7c112fe841b2990}"

uv run python - <<PY
from huggingface_hub import HfApi
api = HfApi()
try:
    r = api.add_collection_item(
        collection_slug="$COLLECTION_SLUG",
        item_id="$DATASET_ID",
        item_type="dataset",
        note="""$NOTE""" or None,
    )
    print("added to collection:")
    for it in r.items:
        print(f"  - {it.item_id} ({it.item_type})")
except Exception as exc:
    print(f"error: {exc}")
PY
