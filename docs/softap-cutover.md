# SoftAP 切换清单（192.168.207.x → 192.168.4.x）

前提：香橙派已跑 `deploy/softap/setup-softap.sh`，wlan0 = 192.168.4.1，SSID `xiaochen-ai`。

固定地址规划：

| 设备 | IP | 依据 |
|------|----|------|
| 香橙派 wlan0 | 192.168.4.1 | 静态 |
| xiaochen-node1 | 192.168.4.10 | dnsmasq MAC 绑定 |
| xiaochen-relay1（客厅灯） | 192.168.4.21 | 58:bf:25:4d:0e:60 |
| xiaochen-relay2（卧室灯） | 192.168.4.22 | 58:bf:25:4c:62:81 |
| xiaochen-curtain1（客厅窗帘） | 192.168.4.23 | e8:68:e7:77:0b:da |
| xiaochen-fan1（卧室风扇） | 192.168.4.24 | ec:fa:bc:14:96:96 |

## 要改的地址（搜 `192.168.` 全查）

1. `firmware/xiaochen-node1.yaml`：wifi 改 SSID `xiaochen-ai`（密码进 secrets.yaml）；`mqtt` broker 改 `192.168.4.1`
2. `firmware/node_audio.h`：两处 `http://192.168.207.150:8899/...` 改 `http://192.168.4.1:8899/...`
3. 节点固件改完 → `build_node1.sh` 重建 → md5 字节校验 → OTA（`firmware/do_ota.py`）
4. Tasmota ×4：`MqttHost 192.168.4.1`（串口控制台或 Web），WiFi 改 `xiaochen-ai`
5. PC 辅助脚本 `firmware/mqpub.py / mqplay.py / mqtt_push_tts.py / t9890.py / sshop.py`：目标 IP 改 `192.168.4.1`（或节点 `.10`）
6. 派上 `relay-mqtt.conf`：MQTT_HOST 本就是 127.0.0.1，**不用改**
7. `deploy/softap/dnsmasq.conf`：补 node1 的 MAC 绑定行

## 验证顺序

1. 各设备已挂上 `xiaochen-ai`（dnsmasq leases 看 MAC 对 IP）
2. broker 通：`mosquitto_sub`/`mqpub` 互发
3. 节点 OTA 后 ping 192.168.4.10，唤醒 → ASR → TTS 回放全链
4. 语音开关灯/窗帘各一轮

## 回滚

各文件改回 192.168.207.x，设备回原热点；hostapd/dnsmasq 停用即可。
