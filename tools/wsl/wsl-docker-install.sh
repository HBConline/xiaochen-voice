#!/usr/bin/env bash
# WSL Ubuntu 里装 docker-ce + buildx + qemu（arm64 交叉构建用）
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y ca-certificates curl gnupg qemu-user-static

# docker 官方 apt 源
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 启动 dockerd（WSL 无 systemd 时后台起）
if command -v systemctl >/dev/null && systemctl start docker 2>/dev/null; then
  echo "dockerd via systemd"
else
  nohup dockerd >/var/log/dockerd.log 2>&1 &
  sleep 4
fi

docker version
docker buildx ls
echo "[ok] docker ready in WSL"
