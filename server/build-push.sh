#!/usr/bin/env bash
# 一键构建 + 推 GHCR（arm64，香橙派/云构建机上跑最好——原生架构不用模拟）
# 用法：bash build-push.sh [tag]   默认 v0.1
# 需要：docker buildx；先 docker login ghcr.io -u HBConline --password-stdin（token 要 write:packages）
set -euo pipefail
TAG="${1:-v0.1}"
IMAGE="ghcr.io/hbconline/xiaochen-voice-server:${TAG}"

cd "$(dirname "$0")"
docker buildx build \
  --platform linux/arm64 \
  -t "$IMAGE" \
  --push \
  .
echo "[ok] pushed $IMAGE"
