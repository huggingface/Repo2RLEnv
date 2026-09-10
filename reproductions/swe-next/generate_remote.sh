#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/swe-next/venv/bin:$PATH"
export PYTHONPATH=/work/recipes/swe-next/telemetry:/work/recipes/runtime
export REPRO_API_RESPONSES=/evidence/swe-next/openai-api.jsonl
export ANTHROPIC_BASE_URL=https://api.anthropic.com
export PR_PIPELINE_MAX_PARALLEL=1 PR_MAX_PRS_PER_REPO=30
export PR_MAX_COMMITS_PER_REPO="${REPRO_MAX_COMMITS:-1}"
export PR_N_CPUS=1 PR_N_CPUS_DOCKER=1 R2EGYM_ENV_PROFILE_MAX_PROFILES=1
export PR_GEN_TRAJ=0 PR_GEN_SFT_TRAJ=0 PR_TRAJ_MAX_PER_REPO=1
export PR_PUSH_IMAGES=0 PR_REQUIRE_HUB_DOCKER_IMAGE=0 PR_BUILD_IMAGES=1
export PR_CLEANUP_AFTER_REPO=0 PR_PRUNE_BUILD_CACHE_AFTER_REPO=0 PR_SHRINK_REPOS=0
export PR_REQUIRE_PROBLEM_STATEMENT=1
cd /work/swe-next/upstream
if [ ! -e .venv ]; then ln -s /work/swe-next/venv .venv; fi
python - <<'PY'
import json, os, subprocess
from pathlib import Path
settings={k:v for k,v in os.environ.items() if k.startswith('PR_') or k=='R2EGYM_ENV_PROFILE_MAX_PROFILES'}
Path('/evidence/swe-next/pilot-settings.json').write_text(json.dumps(settings,indent=2))
repo=os.environ.get('REPRO_INPUT_REPO','psf/black')
result=subprocess.run(['zsh','run_pr_pipeline.zsh',repo],check=False,timeout=1700)
datasets=list(Path('outputs/repo_datasets').glob('*_pr.jsonl'))
summary={'native_returncode':result.returncode,'datasets':{str(p):sum(bool(l.strip()) for l in p.read_text().splitlines()) for p in datasets}}
Path('/evidence/swe-next'/Path(repo.replace('/','-')+'-generation-summary.json')).write_text(json.dumps(summary,indent=2))
print(json.dumps(summary))
result.check_returncode()
PY
