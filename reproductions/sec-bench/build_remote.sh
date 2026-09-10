#!/usr/bin/env bash
set -euo pipefail
export PATH="/work/sec-bench/venv/bin:$PATH"
export PYTHONPATH=/work/sec-bench/upstream
cd /work/sec-bench/upstream
python -m secb.preprocessor.build_instance_images --input-file /work/sec-bench/seed.jsonl \
 --ids njs.cve-2022-32414 --workers 1
docker image inspect hwiwonlee/secb.x86_64.njs.cve-2022-32414:latest > /evidence/sec-bench/native-image.json
