#!/usr/bin/env bash
set -euo pipefail
# Pin the upstream project's tool rather than rewriting SIF generation to Docker.
mkdir -p /work/apptainer /evidence/apptainer
cd /work/apptainer
curl -fL --retry 2 -o apptainer.deb https://github.com/apptainer/apptainer/releases/download/v1.5.3/apptainer_1.5.3_amd64.deb
curl -fL --retry 2 -o apptainer-suid.deb https://github.com/apptainer/apptainer/releases/download/v1.5.3/apptainer-suid_1.5.3_amd64.deb
sha256sum *.deb > /evidence/apptainer/packages.sha256
apt-get update -qq
apt-get install -y ./apptainer.deb ./apptainer-suid.deb
apptainer version | tee /evidence/apptainer/version.txt
apptainer config fakeroot --add root
apptainer pull ubuntu_22.04.sif docker://ubuntu:22.04
apptainer exec --fakeroot --userns --writable-tmpfs --cleanenv ubuntu_22.04.sif bash -c 'touch /tmp/native-apptainer-smoke; test -f /tmp/native-apptainer-smoke; echo remote-apptainer-pass'
date -u +%FT%TZ > /evidence/apptainer/smoke-passed.txt
