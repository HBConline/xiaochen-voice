# 小宸同学 · 全离线家庭语音助手

一套自研的端—边协同语音助手：ESP32-S3 采音节点常驻唤醒，香橙派 4 Pro 边缘大脑完成识别、决策与合成，MQTT 驱动 ESP8266 继电器控制家电，构成「唤醒—识别—决策—执行—播报」闭环，全程局域网运行、不依赖云端。

## 架构

```
ESP32-S3 节点                香橙派 4 Pro（纯 CPU）              ESP8266 ×N
┌──────────────┐   WiFi   ┌──────────────────────────┐  MQTT  ┌─────────────┐
│ microWakeWord │ ───────> │ SenseVoice ASR (int8)     │ ─────> │ Tasmota     │
│ 常驻唤醒/录音  │  音频上传 │ 两级决策                    │  指令   │ 继电器/家电  │
│ TFLite Micro  │ <─────── │  ├ 微规则直达简单指令        │ <───── │             │
│ 播放 TTS 应答  │  TTS 推流 │  └ Qwen3-0.6B (Q4, llama.cpp)│ 状态    │             │
└──────────────┘          │     + MCP 工具调用          │        └─────────────┘
                          │ Matcha TTS (int8)          │
                          │ hostapd 自建 AP / 固定 IP   │
                          └──────────────────────────┘
```

- **端侧**：microWakeWord 混合网络以真实人声语料 TensorFlow 从头训练，PTQ int8 量化经 TFLite Micro 在 ESP32-S3（FreeRTOS）常驻流式推理；OTA 统一升级 + 固件字节级校验保障版本一致。
- **边缘侧**：Sherpa-ONNX int8 运行时（SenseVoice ASR / Silero VAD / Matcha TTS）；意图分两级——微规则直达设备、零推理开销，复杂请求由 Qwen3-0.6B（Q4_K_M，llama.cpp CPU 推理）解析；提示词工程定义人设与工具调用规范，经 MCP 协议把设备与系统能力封装为标准化工具，大模型自主选工具生成设备指令。
- **网络**：香橙派经 hostapd 自建 WiFi AP，节点与继电器设备统一入网，按 MAC 绑定固定 IP，断外网可用。部署套件见 `deploy/softap/`，切换步骤见 `docs/softap-cutover.md`。

## 目录

| 目录 | 内容 |
|------|------|
| `firmware/` | ESP32-S3 节点固件源码（ESPHome yaml + 自定义 C++ 音频/唤醒组件 + OTA 脚本 + 唤醒模型 xiaochen.tflite） |
| `server/` | 香橙派服务端：ASR/意图/TTS 管线、MCP 服务、设备层、测试套件、systemd 单元 |
| `training/` | 唤醒词训练脚本（数据构建、切分、TFLite 转换、A/B 评分） |
| `tools/` | ESP-01 Tasmota 串口配置、网络扫描、继电器测试脚本 |
| `deploy/softap/` | 自建 WiFi AP 部署套件（hostapd/dnsmasq/MAC 固定 IP + 一键脚本） |
| `docs/` | 部署交接文档、唤醒词训练指南 |

## 模型文件说明

仓库只含固件级唤醒模型（`firmware/xiaochen.tflite`，约 75KB）。以下大体积模型**不在仓库内**，按 `docs/` 文档在设备侧放置：

- `server/brain/Qwen3-0.6B-xiaochen-Q4_K_M.gguf`（约 380MB）
- Sherpa-ONNX ASR/TTS/VAD int8 模型目录

## 配置

固件 WiFi/MQTT 走 `firmware/secrets.yaml`（**不入库**），格式：

```yaml
wifi_ssid: "your-ssid"
wifi_password: "your-password"
```

## 测试

```bash
cd server/app && python -m pytest        # 意图规则 / 设备层 / MCP 测试套件
python tools/test_curtain_relay.py       # 继电器绑定回归
```
