#!/usr/bin/env bash
# 在香橙派上把 wlan0 变成 xiaochen-ai 热点（Ubuntu 22.04）。
# 用法：sudo AP_PASS='你的WPA2密码' bash setup-softap.sh
# 密码只进 /etc/hostapd/hostapd.conf，不进 git。
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "need root"; exit 1; }
[ -n "${AP_PASS:-}" ] || { echo "set AP_PASS env"; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"

apt-get install -y hostapd dnsmasq

# wlan0 静态地址 192.168.4.1（NetworkManager 让位给 hostapd）
cat > /etc/NetworkManager/conf.d/99-unmanaged-wlan0.conf <<'EOF'
[keyfile]
unmanaged-devices=interface-name:wlan0
EOF
systemctl restart NetworkManager

ip addr flush dev wlan0 || true
ip addr add 192.168.4.1/24 dev wlan0
ip link set wlan0 up
# 开机持久化
grep -q 'xiaochen-softap' /etc/rc.local 2>/dev/null || cat >> /etc/rc.local <<'EOF'
ip addr flush dev wlan0; ip addr add 192.168.4.1/24 dev wlan0; ip link set wlan0 up # xiaochen-softap
EOF

sed "s/__AP_PASS__/${AP_PASS}/" "$HERE/hostapd.conf" > /etc/hostapd/hostapd.conf
chmod 600 /etc/hostapd/hostapd.conf
cp "$HERE/dnsmasq.conf" /etc/dnsmasq.d/xiaochen.conf

systemctl unmask hostapd
systemctl enable --now hostapd
systemctl enable --now dnsmasq

echo "[ok] AP ssid=xiaochen-ai ip=192.168.4.1"
echo "next: 设备入网后按 docs/softap-cutover.md 改各处 192.168.4.x 地址"
