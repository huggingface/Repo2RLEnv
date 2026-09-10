#!/usr/bin/env bash
set -euo pipefail
apptainer config fakeroot --add root
cd /work/apptainer
apptainer exec --fakeroot --userns --writable-tmpfs --cleanenv ubuntu_22.04.sif bash -c 'touch /tmp/native-apptainer-smoke; test -f /tmp/native-apptainer-smoke; echo remote-apptainer-pass'
date -u +%FT%TZ > /evidence/apptainer/smoke-passed.txt
