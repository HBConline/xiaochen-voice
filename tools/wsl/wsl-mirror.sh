#!/usr/bin/env bash
# WSL docker 配国内镜像加速（Docker Hub 被墙的根治），然后重启 dockerd
set -euxo pipefail
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.net",
    "https://hub.rat.dev",
    "https://docker.1ms.run"
  ]
}
EOF
pkill dockerd || true
sleep 2
nohup dockerd >/var/log/dockerd.log 2>&1 &
sleep 5
docker info 2>/dev/null | grep -A4 'Registry Mirrors' || docker info | tail -5
echo "[ok] mirrors configured"
