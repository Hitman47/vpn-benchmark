#!/usr/bin/env bash
# Construit l'image et la publie sur un registre (equivalent de publish.ps1).
#   ./publish.sh ghcr.io/mon-compte/vpn-benchmark:latest
set -euo pipefail
cd "$(dirname "$0")"
IMAGE="${1:?usage: ./publish.sh <registre>/<image>:<tag>}"
docker build -t "$IMAGE" .
docker push "$IMAGE"
echo "Image publiee : $IMAGE"
echo "Dans Portainer : colle docker-compose.registry.yml et mets BENCH_IMAGE=$IMAGE"
