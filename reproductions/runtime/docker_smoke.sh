#!/usr/bin/env bash
set -euo pipefail
for attempt in $(seq 1 60); do
  if docker info > /evidence/docker-info.txt 2>&1; then break; fi
  sleep 1
done
docker info > /evidence/docker-info.txt
docker compose version
mkdir -p /work/runtime-smoke
cat > /work/runtime-smoke/Dockerfile <<'EOF'
FROM python:3.12-slim
RUN python -c 'import pathlib; pathlib.Path("/built-remotely").write_text("yes")'
CMD ["python", "-c", "import pathlib; assert pathlib.Path('/built-remotely').read_text() == 'yes'; print('remote-docker-build-run-pass')"]
EOF
docker build -t reproduction-runtime-smoke /work/runtime-smoke
docker run --rm --network none reproduction-runtime-smoke
date -u +%FT%TZ > /evidence/docker-smoke-passed.txt
