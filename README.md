# 小宸同学 · 全离线家庭语音助手

> ESP32-S3 端侧唤醒 + 香橙派边缘大脑（ASR / 两级决策 / Qwen3-0.6B / TTS）+ ESP8266 继电器执行，「唤醒—识别—决策—执行—播报」端—边协同秒级闭环，全程局域网运行、不依赖云端。
>
> 🎬 演示视频 [bilibili](https://b23.tv/lUWWbtx) · 📦 模型大文件 [Releases](https://github.com/HBConline/xiaochen-voice/releases) · 🐳 镜像 [`ghcr.io/hbconline/xiaochen-voice-server`](https://github.com/users/HBConline/packages/container/package/xiaochen-voice-server) · 🛒 采购 [`docs/BOM.md`](docs/BOM.md) · 🔌 接线 [`docs/WIRING.md`](docs/WIRING.md)

一套自研的端—边协同语音助手系统。采音节点常驻唤醒词检测，喊一声「小宸同学」即进入对话；语音识别、意图理解、大模型决策、语音合成都发生在本地的香橙派 4 Pro 上；识别结果经 MQTT 驱动继电器控制灯、窗帘、风扇等家电，应答语音推回节点播放。覆盖「数据构建—模型训练—量化转换—部署—真机迭代」的完整端侧模型部署链路。

---

## 目录

- [功能特性](#功能特性)
- [系统架构](#系统架构)
- [硬件清单](#硬件清单)
- [技术栈](#技术栈)
- [原理解读](#原理解读)
- [目录结构](#目录结构)
- [快速开始](#快速开始)
- [测试](#测试)
- [关键设计决策与踩坑记录](#关键设计决策与踩坑记录)
- [模型文件说明](#模型文件说明)
- [路线图](#路线图)
- [常见问题（FAQ）](#常见问题faq)

---

## 功能特性

- **离线唤醒**：自研唤醒词「小宸同学」，microWakeWord 混合网络 int8 量化，ESP32-S3 上 TFLite Micro 常驻流式推理，断网也能唤醒
- **语音控制家电**：灯（客厅/卧室）、窗帘（客厅）、风扇（卧室），设备绑定按房间隔离，说「打开窗帘」不会误动别人家的窗帘
- **两级决策**：简单指令微规则直达（毫秒级、零推理开销）；复杂请求 Qwen3-0.6B 大模型解析，经 MCP 协议调用标准化工具
- **自然对话**：提示词工程定义人设（小宸同学）、对话策略与工具调用规范，闲聊走大模型长文本应答
- **语音播报**：Matcha TTS 流式合成，音频分片经 MQTT 推回节点边传边播
- **网络自治**：香橙派自建 WiFi AP，全部设备 MAC 绑定固定 IP，断外网整套照跑
- **可视反馈**：节点侧 ST7789 彩屏显示对话状态（可选组件）
- **工程质量**：6 个自动化测试套件（意图规则 98/98、设备层、MCP、语音状态机），OTA 升级带固件字节级校验

## 系统架构

```
┌─────────────────┐        ┌──────────────────────────────────────┐        ┌─────────────────┐
│  ESP32-S3 节点   │  WiFi  │           香橙派 4 Pro（纯 CPU）        │  MQTT  │  ESP8266 ×N     │
│  xiaochen-node1 │        │              192.168.4.1               │        │  Tasmota 继电器  │
│                 │ 音频上传 │                                      │  指令   │                 │
│ microWakeWord   │ ─────> │  ① SenseVoice ASR（int8 识别）         │ ─────> │ relay1  客厅灯   │
│  常驻唤醒检测     │        │  ② Silero VAD（断句）                  │        │ relay2  卧室灯   │
│ I2S 麦克风采集    │ <───── │  ③ 两级决策                           │ <───── │ curtain1 客厅窗帘│
│ 扬声器播 TTS 应答 │ TTS 推流 │     ├ 微规则 → 直达设备指令            │  状态   │ fan1    卧室风扇 │
│ ST7789 状态屏    │        │     └ Qwen3-0.6B → MCP 工具调用        │        │                 │
│ FreeRTOS 任务    │        │  ④ Matcha TTS（int8 合成）              │        └─────────────────┘
└─────────────────┘        │  ⑤ MQTT broker / Flask 音频接口         │
                            │  ⑥ hostapd 自建 AP + dnsmasq MAC 固定IP │
                            └──────────────────────────────────────┘
```

### 一次完整交互的数据流

1. **唤醒**：节点上 microWakeWord 持续分析麦克风帧，滑动窗内唤醒概率和超过阈值（cutoff=0.07）触发
2. **本地应答**：立即播放「我在，你说」提示音（不等服务器，听感零延迟），同时开始录音
3. **录音结束判定**：起声门控——检测到人开口才起算静音收尾；没开口 2.5 秒放弃归位（防假唤醒占用通道）
4. **上传**：录音 ≥0.3 秒才上传（过滤假唤醒碎音），PCM 16kHz/S16LE 经 HTTP POST 到 Flask 音频接口
5. **识别**：SenseVoice ASR（int8）转文本，Silero VAD 辅助断句
6. **决策**：
   - 简单设备指令（「打开客厅灯」）→ 微规则匹配 → 直接生成设备指令，**零推理开销**
   - 复杂请求（「帮我把家里弄亮一点」）→ Qwen3-0.6B（Q4_K_M，llama.cpp CPU 推理）解析，经 MCP 协议从工具白名单中选择设备/系统工具生成指令
   - 纯闲聊 → 大模型直接生成应答文本
7. **执行**：设备指令经 MQTT 发布到 Tasmota 继电器（`cmnd/<topic>/POWER`），设备状态回写
8. **播报**：应答文本由 Matcha TTS 合成音频，经 MQTT begin/chunk/end 三段协议推回节点播放
9. **展示**：对话文本上 ST7789 屏（可选）

## 硬件清单

> 采购明细、参考价与注意事项见 [`docs/BOM.md`](docs/BOM.md)；接线图见 [`docs/WIRING.md`](docs/WIRING.md)。

| 硬件 | 角色 | 关键规格 | 数量 |
|------|------|---------|------|
| ESP32-S3 开发板 | 采音节点 | 双核 LX7 240MHz、512KB SRAM、I2S、WiFi | 1 |
| 香橙派 4 Pro | 边缘大脑 | 8 核 ARM、4GB RAM、纯 CPU 无 GPU、Ubuntu 22.04 | 1 |
| ESP-01（ESP8266）+ 继电器板 | 家电执行器 | 单核 80MHz、GPIO0 接继电器、Tasmota-lite 14.6 | 4 |
| I2S 数字麦克风 INMP441（节点） | 采集 | 16kHz / S16LE / 单声道 | 1 |
| 喇叭 + MAX98357 I2S 功放（节点） | 播报 | I2S 输出 | 1 |
| ST7789 1.54 寸彩屏 | 对话显示（可选） | 240×240 SPI | 1 |
| CH340 USB-TTL | 刷机/串口配置 | 3.3V！5V 会烧模块 | 1 |

## 技术栈

| 层 | 技术 | 用途 |
|----|------|------|
| 端侧硬件 | ESP32-S3 / ESP8266、ESP-IDF、FreeRTOS | 采音节点固件（多任务实时音频流）、继电器执行器 |
| 端侧构建 | ESPHome、PlatformIO、C++ | yaml 声明式配置 + 自定义音频/唤醒组件 + 构建烧录 |
| 唤醒模型 | microWakeWord、TensorFlow、TFLite Micro | 混合网络从头训练、PTQ int8 量化、MCU 常驻流式推理 |
| 语音运行时 | Sherpa-ONNX（SenseVoice ASR / Silero VAD / Matcha TTS） | int8 量化语音识别、断句、合成，全本地 |
| 大模型 | Qwen3-0.6B、llama.cpp、GGUF Q4_K_M | 长尾请求解析、闲聊应答，CPU 推理 |
| 工具调用 | 提示词工程、MCP | 人设与调用规范、设备能力标准化封装 |
| 通信 | MQTT、HTTP（Flask） | 指令下发/状态上报/TTS 推流、音频上传接口 |
| 网络 | hostapd、dnsmasq | 自建 WiFi AP、MAC 绑定固定 IP |
| 运维 | Linux、systemd、Python | 服务化部署、测试套件、配置脚本 |
| 设备固件 | Tasmota 14.6 lite | ESP-01 继电器成熟固件，MQTT 接入 |

## 原理解读

### 端侧唤醒：microWakeWord 混合网络

- **模型**：mixednet（时域卷积 + 频域卷积混合结构），参数量小（百 KB 级），专为 MCU 设计
- **判定**：每帧音频特征输出唤醒概率，最近 N 帧（滑动窗 30）概率求和 > `cutoff × 窗长` 触发唤醒。cutoff 是误唤醒/漏唤醒的平衡旋钮（本项目标定 0.07）
- **训练**：真实人声正样本 + 背景噪声/近似词困难负样本（「小明」「小张」「同学们好」），TensorFlow 从头训练（r5–r9 多轮迭代），PTQ int8 量化导出 tflite
- **踩坑重点**：合成语音（TTS）训练只能叫醒 TTS 自己，真人零唤醒——域过拟合。解法是真实人声重建训练集 + A/B 对照验证，真机是唯一裁判

### 量化：为什么是 int8 / Q4

| 方案 | 做法 | 用在哪 |
|------|------|--------|
| PTQ int8 | 训练后量化，`q = round(x / scale)`，体积 ÷4 | 唤醒模型、ASR/TTS/VAD |
| Q4_K_M | GGUF 4-bit 分块量化（k-quant） | Qwen3-0.6B（380MB 可驻留 4GB 板） |

不量化的话 fp32 唤醒模型 MCU 跑不动，7B 级 LLM 更是放不下。量化是「多模型常驻纯 CPU 小板」的前提。

### 两级决策：规则保底、模型扩展

- **微规则层**：文本规范化（norm）→ 模式匹配 → 设备指令。毫秒级、100% 可控，覆盖开关灯/窗帘/风扇等高频指令
- **大模型层**：Qwen3-0.6B 解析复杂/长尾请求，提示词工程钉死输出格式（JSON 工具调用），经 MCP 协议调用标准化工具（工具参数过白名单校验后才驱动硬件），解析失败降级反问澄清
- **为什么分级**：全走大模型延迟高且有随机性，高频开关指令要毫秒级确定；全走规则覆盖不了长尾。规则保底、模型扩展是资源受限下的最优解

### MCP：设备能力标准化

设备与系统能力（开灯、关窗帘、查状态）封装为 MCP tool（schema 定义入参出参），好处：

1. 工具定义与模型解耦——换模型不动工具层
2. 工具可被多客户端复用——语音、网页、其他 Agent 同一套工具
3. 新设备只需注册新 tool，大模型代码零改动

### 录音的两个细节（工程味十足）

- **起声门控**：静音收尾计时从「检测到开口」起算，容忍喊完唤醒词后的犹豫（否则 0.8 秒静音就把话截断，表现为「唤醒了但没反应」）
- **快速归位**：没等到开口 2.5 秒放弃，假唤醒快速让出通道，不挤掉真唤醒

### 网络自治：SoftAP

手机热点的 DHCP 子网会漂（124.x → 207.x），固件里写死的 IP 全变砖。根治方案：香橙派 hostapd 自建 AP（`xiaochen-ai`）+ dnsmasq 按 MAC 绑定固定 IP，所有设备地址永久稳定，断外网可用。部署套件见 `deploy/softap/`。

## 目录结构

```
xiaochen-voice/
├── README.md                     # 本文
├── firmware/                     # ESP32-S3 节点固件
│   ├── xiaochen-node1.yaml       # ESPHome 主配置（WiFi/MQTT/microWakeWord/I2S/屏幕）
│   ├── secrets.yaml.example      # WiFi 凭据模板（cp 为 secrets.yaml 填写，永不入库）
│   ├── xiaochen.json             # 唤醒模型描述（cutoff 0.07、滑动窗 30、tensor arena 120KB）
│   ├── xiaochen.tflite           # 唤醒模型（int8，约 75KB，r9a 真人声训练版）
│   ├── node_audio.h              # 音频核心：唤醒任务、起声门控录音、HTTP 上传、TTS 接收播放
│   ├── node_play.h               # 播放相关（提示音/TTS 流）
│   ├── wozai_pcm.h               # 「我在，你说」提示音 PCM 数据
│   ├── micro_wake_word.cpp.mwwprobe  # 打过补丁的 microWakeWord 组件（概率日志等）
│   ├── build_node1.sh            # 构建脚本（esphome codegen → 补丁拷入 → pio run）
│   ├── do_ota.py                 # OTA 推送（esphome.espota2，端口 3232）
│   ├── mqpub.py / mqplay.py      # PC 侧 MQTT 调试工具
│   ├── mqtt_push_tts.py          # TTS 音频分片推送工具
│   ├── sshop.py                  # SSH 到香橙派跑命令（密码走 OP_PASS 环境变量）
│   ├── t9890.py / read_logs.py   # 调试辅助
│   └── mic-debug.yaml / test-*.yaml / xiaochen-debug.json  # 调试配置
├── server/                       # 香橙派服务端（Python）
│   ├── app/
│   │   ├── wakeword_server.py    # 服务入口：音频接口、麦克风采集环、管线编排
│   │   ├── intent.py             # 意图入口：文本 → 两级决策
│   │   ├── microrules.py         # B3 微规则引擎（98 条规则测试全绿）
│   │   ├── norm.py               # 文本规范化
│   │   ├── voice_brain.py        # Qwen3-0.6B 大脑（llama.cpp 对接、提示词工程、工具调用）
│   │   ├── mcp_pool.py           # MCP 工具池调度
│   │   ├── servers/home_mcp.py   # MCP server：设备与系统能力 → 标准化工具
│   │   ├── device_layer.py       # 设备层：房间绑定、状态机、MQTT 继电器后端
│   │   ├── mqtt_bridge.py        # MQTT 桥接（begin/chunk/end TTS 推流等）
│   │   ├── tts.py                # Matcha TTS 合成
│   │   ├── scenes.py             # 场景模式（回家模式等）
│   │   ├── session.py / voice_state.py / dialogue_view.py  # 会话、状态机、对话视图
│   │   ├── st7789_display.py     # 屏幕显示服务
│   │   ├── home_config.py / gen_dataset.py                 # 配置、数据生成
│   │   ├── static/               # Web 调试页（index/play/realtime/wakeword）
│   │   └── test_*.py             # 测试套件 ×8
│   ├── scripts/                  # 派上启动脚本
│   │   ├── start_llama.sh        # llama-server（Qwen3-0.6B 大脑，-c 2048 --parallel 1）
│   │   └── start_server.sh       # 语音管线（注入 BRAIN_URL / TTS_THREADS 等）
│   ├── Dockerfile                # voice 管线镜像（GHCR: xiaochen-voice-server）
│   ├── docker-compose.yml        # brain + voice 双服务（模型挂载，host 网络）
│   ├── requirements.txt          # 服务端 Python 依赖
│   ├── build-push.sh             # arm64 构建并推送 GHCR
│   └── deploy/
│       ├── chenvoice-*.service   # systemd 单元（brain / voice / display）
│       └── softap/               # ★ 网络自治部署套件
│           ├── hostapd.conf      # AP 配置（SSID xiaochen-ai，密码占位）
│           ├── dnsmasq.conf      # DHCP + MAC 固定 IP（.10/.21/.22/.23/.24）
│           └── setup-softap.sh   # 派上一键安装（AP_PASS 环境变量传密码）
├── training/                     # 唤醒词训练（D:/mww 脚本收编）
│   ├── gen_pos_v2.py / gen_neg_v2.py / gen_neg_zh.py       # 正/负样本生成
│   ├── gen_neg_nearmiss.py / process_nearmiss.py           # 困难负例（小明/小张/同学们好）
│   ├── cut_real_pos.py / segment_v2.py / split_*.py        # 真人声切分与数据构建
│   ├── build_features.py         # 特征构建
│   ├── conv_tflite.py            # int8 TFLite 导出（stateless 修复版）
│   ├── score_ab.py               # A/B 对照评分（真机验证）
│   ├── replay_wake.py / test_distractors.py                # 唤醒回放/干扰词测试
│   └── reapply_mww_patch.py      # microWakeWord 补丁重放
├── tools/                        # ESP-01 / 继电器工具
│   ├── cfg_relay2.py             # Tasmota 串口一键配置（WiFi/MQTT/模板，密码自动打码）
│   ├── listen_serial.py          # 串口监听
│   ├── scan_fan.py               # 子网扫描找设备（按 MAC）
│   ├── patch_curtain_binding.py  # device_layer 设备级绑定补丁
│   └── test_curtain_relay.py     # 窗帘绑定回归测试（11 项）
└── docs/
    ├── softap-cutover.md         # ★ SoftAP 切换清单（地址迁移/验证/回滚）
    ├── WIRING.md                 # ★ 接线图（节点音频/ST7789 屏/ESP-01 刷机与继电器）
    ├── BOM.md                    # 采购清单（参考价与避坑）
    ├── HANDOFF-ORANGEPI.md       # 香橙派部署交接文档
    └── MWW-TRAINING-GUIDE.md     # 唤醒词训练指南
```

## 快速开始

### 环境要求

- 香橙派 4 Pro（Ubuntu 22.04 aarch64，Python 3.10，纯 CPU）
- PC：Python 3.10+、PlatformIO、esphome（构建固件）
- conda 环境 `mww`（唤醒词训练，依赖 TensorFlow）

### 1. 服务端部署（香橙派）

```bash
# 同步 server/ 到派（内容落位到 ~/chenvoice/ 根，与 systemd 单元路径一致）
ssh orangepi@<派IP> 'mkdir -p ~/chenvoice'
scp -r server/* orangepi@<派IP>:~/chenvoice/

# 服务端依赖
pip install -r ~/chenvoice/requirements.txt

# 放置模型（不在本仓库，见「模型文件说明」）
#   ~/chenvoice/brain/Qwen3-0.6B-xiaochen-Q4_K_M.gguf
#   Sherpa-ONNX ASR/TTS/VAD int8 模型目录

# 启动（llama.cpp 先起，服务后起）
bash ~/chenvoice/scripts/start_llama.sh      # -c 2048 --parallel 1 -fa on
bash ~/chenvoice/scripts/start_server.sh     # 导出 BRAIN_URL / TTS_THREADS 等环境变量

# 服务化（systemd 三单元：brain / voice / display）
sudo cp ~/chenvoice/deploy/*.service /etc/systemd/system/
sudo systemctl enable --now chenvoice-brain chenvoice-voice
```

配置通过环境变量注入（systemd drop-in），关键项：

```ini
DEVICE_BACKEND=mqtt
MQTT_HOST=127.0.0.1
MQTT_RELAY_TOPICS=living_room=cmnd/xiaochen-relay1/POWER,bedroom=cmnd/xiaochen-relay2/POWER,curtain=cmnd/xiaochen-curtain1/POWER,curtain:living_room=cmnd/xiaochen-curtain1/POWER
```

**容器化部署（可选）**：`server/` 自带 `Dockerfile` + `docker-compose.yml`（voice 管线 + llama.cpp brain 双服务，host 网络），模型放 `server/models/` 挂载进容器：

```bash
# 直接拉已发布镜像（免本地构建，linux/arm64）
docker pull ghcr.io/hbconline/xiaochen-voice-server:v0.1

cd server/
# models/ 布局见 docker-compose.yml 头部注释
docker compose up -d            # 镜像在本地就用现成的，缺了才构建
# 发布镜像到 GHCR：bash build-push.sh v0.1
```

### 2. 固件构建与烧录（ESP32-S3）

```bash
cd firmware/
cp secrets.yaml.example secrets.yaml   # 填 WiFi 账号密码（永不入库）
bash build_node1.sh                    # esphome codegen → 组件补丁 → pio run
```

首次烧录走 USB（esphome run / pio run -t upload）；之后升级走 OTA：

```bash
python do_ota.py                       # espota2 推 192.168.x.x:3232
```

**OTA 前必做固件字节级校验**：比对构建产物内嵌的模型字节与 `xiaochen.tflite` 的 md5 一致（防静默刷旧包）。

### 3. 继电器板配置（ESP-01 / Tasmota）

接线（TTL 刷机）：CH340 TX↔模块 RXD、RX↔TXD 交叉，**VCC 只接 3.3V**，EN(CH_PD)→3V3，IO0→GND 进下载模式。供电不足必须外部供电（CH340 3.3V 带不动 WiFi）。完整接线图见 [`docs/WIRING.md`](docs/WIRING.md)。

刷 Tasmota-lite 14.6 后串口一键配置：

```bash
python tools/cfg_relay2.py COM5 xiaochen-relay2   # 密码从 secrets.yaml 读取，输出自动打码
```

模板（继电器反相，灯亮灭才正确）：

```json
{"NAME":"ESP01-Relay","GPIO":[256,0,0,0,0,0,0,0],"FLAG":0,"BASE":18}
```

> 坑：Tasmota 模板里继电器代码是 **256（Relay_i）/ 224（Relay）**，不是老版本的 21。写 21 能保存但不建继电器通道。

### 4. 网络自治切换（SoftAP）

见 [`docs/softap-cutover.md`](docs/softap-cutover.md)。概要：

```bash
# 派上
sudo AP_PASS='WPA2密码' bash deploy/softap/setup-softap.sh
# 然后按清单改各处地址：固件 yaml / node_audio.h / Tasmota MqttHost / PC 脚本
```

## 测试

```bash
# 服务端测试套件（香橙派或本地）
cd server/app && python -m pytest

# 各套件单独跑
python -m pytest test_intent_b3.py      # B3 意图规则 98/98
python -m pytest test_microrules.py     # 微规则引擎
python -m pytest test_mqtt_relay.py     # MQTT 继电器后端
python -m pytest test_voice_brain.py    # 大脑决策
python -m pytest test_voice_state.py    # 语音状态机
python -m pytest test_mcp_pool.py       # MCP 工具池

# 继电器绑定回归
python tools/test_curtain_relay.py      # 11 项：房间隔离 / 设备级绑定 / 灯回归

# 唤醒词 A/B 评分
python training/score_ab.py
```

真机验收清单：

1. 唤醒「小宸同学」→ 听到「我在，你说」
2. 「打开客厅灯」/「关闭客厅灯」→ 继电器动作 + TTS 确认
3. 「打开窗帘」（roomless）→ 客厅窗帘；「打开卧室窗帘」→ state-only 不误动
4. 干扰词「小明同学」「同学们好」→ 不唤醒
5. 喊完停顿 2 秒再说话 → 仍能完整录音应答（起声门控生效）

## 关键设计决策与踩坑记录

| 问题 | 教训 | 解法 |
|------|------|------|
| TTS 训练的唤醒模型叫不醒真人 | 仿真指标骗人，域过拟合 | 真实人声重建训练集从头训练，A/B 对照，真机裁判 |
| 唤醒了但没反应 | 假唤醒挤占通道 + 静音截断太狠 | 起声门控 + 2.5s 快速归位 + 录音时长下限 |
| 误唤醒率高 | 阈值一刀切不够 | cutoff 标定 0.07 + 滑动窗 30 + 困难负例训练 |
| esphome upload 静默刷旧包 | 部署不可观测 = 定时炸弹 | OTA 前固件字节级校验（md5 比对） |
| 手机热点子网漂移 | 写死 IP 全变砖 | SoftAP + MAC 固定 IP（deploy/softap/） |
| 「打开窗帘」动错房间的窗帘 | 绑定粒度太粗 | device_layer 设备级绑定（`device:room` + 裸 `device`），无键房间 state-only |
| 4GB 板放不下多模型 | 内存是硬约束 | 全量化（int8/Q4）+ 常驻 + zram + 分级决策 + 限线程 |
| LLM 输出不稳定 | 直接驱动硬件不可接受 | JSON 格式钉死 + 白名单校验 + 失败降级反问 |
| Tasmota 模板继电器代码 21 无效 | 老文档过时 | 用 224/256（`cmnd/<topic>/GPIOs` 查代码表） |
| ESP-01 串口全静默 | 硬件死（常因接过 5V） | 同线材换好板秒通即确认；ROM 引导不应答=没电/晶振/TX 断 |

## 模型文件说明

仓库只含固件级唤醒模型（`firmware/xiaochen.tflite`，约 75KB）。其余大体积文件全部在 **[Releases](https://github.com/HBConline/xiaochen-voice/releases)** 下载，每个附件带 `.md5` 校验文件：

| Release 附件 | 大小 | 放置位置 |
|------|------|---------|
| `Qwen3-0.6B-xiaochen-Q4_K_M.gguf` | 378MB | `~/chenvoice/brain/` |
| `voice-models.zip`（asr/tts/vad） | 308MB | 解压到服务端模型目录（包内 README 有路径） |
| `xiaochen-node1-firmware.bin` | 1MB | OTA 升级用（`firmware/do_ota.py` 推送） |
| `xiaochen-r9a.tflite` | 75KB | 与仓库内固件模型同物（md5 一致） |
| `tasmota-lite-14.6.0.bin` | 500KB | ESP-01 刷机（esptool 烧录） |

下载后先校验：`md5sum -c <文件>.md5`。唤醒词训练数据（真人声/合成样本）不入库不发 Release——含个人声纹，且体积大。

## 路线图

- [x] Docker 镜像发布 GHCR（v0.1，2026-10-29）
- [ ] SoftAP 实际切换（套件已就绪，见 `docs/softap-cutover.md`）
- [ ] 风扇板（ec:fa:bc）串口排查完成 → 配置 `xiaochen-fan1` → 加 `fan`/`fan:bedroom` 绑定
- [ ] r9c 唤醒模型：近似词（小明/小张/沈同学）困难负例扩充后重训
- [ ] ASR 烂文本修复（识别结果偶发 `。`）
- [ ] 多节点 OTA 批量化（节点横向扩展）

## 常见问题（FAQ）

**Q：断网能用吗？**
能。唤醒在节点本地，识别/决策/合成全在香橙派，WiFi AP 也是派自建的。外网只影响「更新模型/代码」。

**Q：响应有多快？**
端到端秒级。唤醒应答「我在，你说」是节点本地播放（零延迟）；常见开关指令不过大模型，毫秒级决策。

**Q：为什么大模型用 0.6B 不用更大的？**
4GB 内存纯 CPU 板的硬约束。0.6B Q4 只占几百 MB，常驻不抢 ASR/TTS 内存；复杂度由「微规则兜高频 + 大模型兜长尾」的架构消化。

**Q：隐私呢？**
音频不出局域网。训练用的真人声数据也不入库。

**Q：想加一个新设备（比如空调）？**
1) ESP-01 刷 Tasmota 接继电器；2) `MQTT_RELAY_TOPICS` 加一行绑定；3) MCP server 注册新 tool（大模型可用）；4) 微规则加高频指令。测试套件加回归用例。

**Q：仓库里为什么没有模型大文件？**
GitHub 单文件 100MB 限制 + 声纹/体积考虑。小的唤醒模型（75KB）在库内，大的按「模型文件说明」自取。
