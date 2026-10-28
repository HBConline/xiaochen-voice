# 宸语 ChenVoice — 香橙派部署交接文档

> **读者：** 香橙派端的 Claude 实例。按本文从上到下执行即可完成部署。
> **交付方：** Windows 开发端（模型微调 + 全链路调速 + 打包）。本文档与 `chenvoice-deploy/` 目录自包含，无需回查开发机。
> **日期：** 2026-10-07

---

## 1. 项目概述

宸语（ChenVoice）是端侧全屋智能语音系统，人设「小宸同学」。全链路在本地 CPU 上跑通：

**KWS 唤醒 → VAD 断句 → SenseVoice ASR → Laya 意图决策 → Qwen3-0.6B 语音大脑生成口语回复 → MOSS-TTS 合成 → 播放**

设计原则：**速度第一**（重复指令毫秒级）、**纯端侧**（无云依赖）、**CPU-only**（目标板无 GPU）。

## 2. 目标硬件与硬约束

- **香橙派 4 Pro**：全志 A733，2×A76 + 6×A55，LPDDR5 **4GB**，无 GPU。
- **铁律：所有组件必须 CPU 可运行。禁止安装 onnxruntime-gpu、CUDA 版 torch、任何 GPU 方案。** ARM64 上 `pip install torch` 默认即 CPU 版，不要加 `--index-url .../cu126` 之类的源。
- Python **3.10+**（代码用了 `str | None` 语法）。

## 3. 双模型架构（核心契约，违反必出错）

| 模型 | 文件 | 职责 |
|------|------|------|
| Qwen3-0.6B-xiaochen (GGUF Q4_K_M, 379MB) | `brain/Qwen3-0.6B-xiaochen-Q4_K_M.gguf` | **纯语音大脑**：只输出口语中文文本，**严禁输出工具调用** |
| Laya 快速决策模型（微调 mmBERT, 1.3GB） | `app/laya-smart-home/` | 自然语言 → 结构化指令（action/room/urgency），一次前向，复合句拆分 |

1. 工具/设备控制归 Laya 意图层（后续接 MQTT/MCP），大脑只负责"怎么说人话"。
2. 上下文通过 Alpaca `input` 字段注入，只有 4 种训练过的形态：
   - `执行结果：switch_light(客厅, on) 成功`
   - `执行结果：switch_light(客厅, off) 成功`
   - `执行结果：set_aircon(客厅, on) 成功`
   - `执行结果：set_aircon(客厅, off) 成功`
   （失败形态：`执行结果：... 失败：设备离线，没有响应`）
3. **场景类话术（"我回来了"、"晚安"等）绝不注入执行结果上下文** —— 训练分布就是无上下文直答，喂了上下文模型会幻觉出设备状态（"客厅的灯已打开，空调25度"）。`voice_brain.build_context` 已实现此规则，不要"优化"掉。
4. 状态查询（带 吗/多少度/状态/查 等标记）同样不注入上下文——没有真实设备状态前绝不编造。
5. 推理参数：`chat_template_kwargs: {"enable_thinking": false}`，temperature 0，`max_tokens: 64`。

## 4. 包结构（`chenvoice-deploy/`，共 2.6GB）

```
chenvoice-deploy/
├── HANDOFF-ORANGEPI.md          本文档
├── requirements.txt             Python 依赖（版本已锁关键包）
├── app/                         语音管线服务（原 sensevoice-sherpa 目录）
│   ├── wakeword_server.py       Flask + WS 主服务（KWS/VAD/ASR/意图/TTS 串起来）
│   ├── intent.py                Laya 意图封装（8 线程 + 256 条 LRU 缓存）
│   ├── tts.py                   MOSS-TTS 合成 + 回复模板 + 回复缓存
│   ├── voice_brain.py           llama-server 客户端（口语回复 + 喊名查表 + 上下文规则）
│   ├── gen_dataset.py           意图问题定义（intent.py 导入 QUESTIONS，**必须保留**）
│   ├── test_voice_brain.py      大脑回归测试（7 条）
│   ├── model.int8.onnx + tokens.txt   SenseVoice ASR (229MB)
│   ├── laya-smart-home/         Laya 微调模型 (1.3GB)
│   ├── static/                  唤醒测试网页 UI
│   ├── test_wavs/               测试音频
│   └── tts_out/prompt_codes.json  音色 prompt 预计算缓存（**必带**，省 ~5s/次编码）
├── kws/                         唤醒词模型（Zipformer KWS 3.3M）+ my_keywords.txt
├── silero-vad/silero_vad.onnx   VAD
├── brain/Qwen3-0.6B-xiaochen-Q4_K_M.gguf   语音大脑 (379MB)
├── moss-tts/                    MOSS-TTS-Nano 运行时（tts.py 的 MOSS_DIR 指这里）
│   ├── onnx_tts_runtime.py / ort_cpu_runtime.py / text_normalization_pipeline.py
│   │   / tts_robust_normalizer_single_script.py / moss_tts_nano/
│   ├── models/MOSS-TTS-Nano-100M-ONNX/ (642MB)
│   ├── models/MOSS-Audio-Tokenizer-Nano-ONNX/ (87MB)
│   └── assets/audio/zh_1.wav    音色参考（克隆音色 = 小宸同学的声音）
└── scripts/
    ├── deploy.sh                一次性环境安装（含 2GB swap）
    ├── build_llamacpp_orangepi.sh  llama.cpp ARM64 构建
    ├── start_llama.sh           启动大脑服务
    ├── start_server.sh          启动语音管线
    └── chenvoice-*.service      systemd 单元（可选开机自启）
```

### 路径契约（不要挪目录！）

代码用**相对路径**找模型，布局被依赖：

- `wakeword_server.py` → `../kws/`、`../silero-vad/`（**`kws`、`silero-vad` 必须与 `app` 平级**）
- `wakeword_server.py` → `app/model.int8.onnx`、`app/tokens.txt`（同目录）
- `intent.py` → `app/laya-smart-home/`（同目录）
- `tts.py` → `MOSS_DIR` 环境变量（`start_server.sh` 默认指 `../moss-tts`）

## 5. 部署步骤

1. **传输整个 `chenvoice-deploy/` 到板子**，建议放 `~/chenvoice`：
   ```bash
   # 在 Windows 端（PowerShell）或用 scp：
   scp -r D:/chenvoice-deploy pi@<板子IP>:~/chenvoice
   # 大文件多，2.6GB；也可 U 盘/网线直传
   ```
2. **装环境**：
   ```bash
   cd ~/chenvoice/scripts
   chmod +x *.sh
   ./deploy.sh          # apt 依赖 + venv + pip + 2GB swap（约 10-20 分钟）
   ```
3. **编译 llama.cpp**（一次性，约 10 分钟）：
   ```bash
   ./build_llamacpp_orangepi.sh
   # 脚本把 llama-server 装到 ~/llama.cpp，把 ~/llama.cpp 加进 PATH，
   # 或在 start_llama.sh 里把 llama-server 换成 ~/llama.cpp/build/bin/llama-server
   ```
   > 注：脚本头部写的 RK3399 是上个项目留下的，构建命令是通用 aarch64 CPU 构建，A733 上照跑。
4. **启动**（顺序：先大脑，后管线）：
   ```bash
   ./start_llama.sh     # 终端 1：llama-server，端口 8080
   ./start_server.sh    # 终端 2：语音管线，端口 8899
   ```
   开机自启（可选）：
   ```bash
   sudo cp chenvoice-*.service /etc/systemd/system/
   # 按实际用户名/路径改 service 里的 User= 和路径，然后：
   sudo systemctl enable --now chenvoice-brain chenvoice-voice
   ```

## 6. 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `MOSS_DIR` | `start_server.sh` 里设为 `<root>/moss-tts` | MOSS-TTS 运行时根目录（代码旧默认 `D:/MOSS-TTS-Nano` 是 Windows 开发路径，Linux 下必须设） |
| `BRAIN_URL` | `http://127.0.0.1:8080/v1/chat/completions` | llama-server 的 OpenAI 兼容端点 |
| `BRAIN_PORT` | `8080` | llama-server 监听端口（`start_llama.sh`） |
| `BRAIN_THREADS` | `6` | llama-server 线程（8 核板留 2 核给管线） |
| `TTS_THREADS` | `2` | MOSS ONNX 线程，**保持 2**（多线程反而抖，实测） |
| `BRAIN_TIMEOUT_S` | `15` | 大脑请求超时 |

## 7. 验收清单

1. **大脑测试**（先确认 llama-server 已起）：
   ```bash
   cd ~/chenvoice/app
   source ../.venv/bin/activate
   python test_voice_brain.py        # 期望 7/7 PASS
   ```
   测试覆盖：确认指令 / 调温 / 场景无幻觉 / 状态查询不编造 / 安全话术 / 失败话术 / 闲聊。PASS = 非空且不含 `tool_call`。
2. **意图测试**：
   ```bash
   python intent.py                  # 打印几条指令的 action/room/urgency
   ```
3. **HTTP 接口**：
   ```bash
   curl -F "file=@test_wavs/某个文件.wav" http://127.0.0.1:8899/transcribe
   curl -X POST http://127.0.0.1:8899/intent -H "Content-Type: application/json" -d '{"text":"打开客厅灯"}'
   ```
   `/intent` 返回应含 `reply_text`、`reply_audio`（wav 文件名，`/tts_audio/<name>` 可取）。
4. **浏览器**：打开 `http://<板子IP>:8899/` 用麦克风喊「小宸同学」走完整链路。

### 性能预期（实测基线 = 12 核笔记本 CPU；ARM 板更慢，属正常）

| 路径 | 笔记本实测 | 香橙派预期 |
|------|-----------|-----------|
| 重复指令（缓存全命中） | 0.1–0.3s | <1s |
| 新短指令（意图+大脑+TTS） | 4.5–5.3s | 8–15s |
| 闲聊 | 5.2s | 10–20s |
| 喊名（查表直答，不走模型） | <0.1s + TTS | 同左 |
| TTS 冷启动（后台预热） | ~17s | 30–60s（含音色编码；**带了 prompt_codes.json 后省一大截**） |

慢的大头永远是 TTS + mmBERT 前向；重复指令靠三层缓存（意图 LRU 256 / 回复 wav LRU 64 / 音色码磁盘缓存）压到毫秒级。**不要为了"提速"动第 8 节的坑。**

## 8. 已知坑（实测踩出来的，每条都有事故记录）

1. **MOSS-TTS 永远禁止 greedy。** `do_sample=False` 路径 `should_continue` 从不触发，跑满 375 帧 = 30 秒垃圾音频 + 97 秒计算。`tts.py` 已固定 `sample_mode="fixed"`，不要改。
2. **帧数封顶必须留着。** 同一文本帧数随 seed 在 16–40 帧波动；`max_new_frames = min(375, len(text)*4+25)` 是唯一硬界限。seed 用 `zlib.crc32(text)`（`hash()` 跨进程不稳定，别换）。
3. **`tts_out/prompt_codes.json` 必须带着。** 音色 prompt（zh_1.wav）编码一次约 5 秒（ARM 更久），这个缓存让每次开机免编码。丢了会自动生成，但第一次会慢。
4. **llama-server 的 `-fa` 必须写成 `-fa on`。** 本 build 的裸 `-fa` 会把后面 `--alias` 当参数值吞掉，起不来：`error while handling argument "-fa": unknown value for --flash-attn: '--alias'`。
5. **场景/查询绝不喂执行结果上下文**（第 3 节第 3、4 条）——这是训练分布的硬规则，喂了必出设备状态幻觉。
6. **喊名走查表，不走模型。** `voice_brain._VOCATIVE_REPLIES` 直答（"小宸同学"→"在，你说。"）。原因：喊名补丁在部分短名上有幻觉尾巴（"在，听我说，最近空调好像离线了…"），且查表瞬时。
7. **英文泄漏兜底 `_scrub_english` 别删。** 模型偶尔吐工具名碎片（"Home"），已映射为中文场景名（"回家模式"）。
8. **4GB 内存很紧。** 全部加载后约 3GB+：llama-server ~500MB + Laya 1.3GB + SenseVoice 230MB + MOSS 会话 ~700MB + torch 运行时。`deploy.sh` 已建 2GB swap。仍紧张就：不跑桌面、关掉不用的服务、或把 Laya 换 fp16 存储（当前 fp32 safetensors）。OOM 杀进程优先怀疑内存，`dmesg | grep -i oom`。
9. **sherpa-onnx 的 VAD 状态机有怪癖**：`is_speech(samples)` 既喂数据又返回状态（注释在 `wakeword_server.py`），升级 sherpa-onnx 小版本前先跑通唤醒。
10. **合成缓存目录 `app/tts_out/` 会增长**（每条回复一个 wav，LRU 只管内存映射，文件不自动删）。定期清理或加 cron 都行，删了不影响正确性。

## 9. 当前边界与后续工作

- **执行结果目前是"假成功"**：MQTT/MCP 设备层尚未接入，`build_context` 对灯/空调指令统一回 `成功`。接设备后把 `voice_brain.build_context` 里的假成功换成真实执行结果即可，训练过的失败话术（`失败：设备离线，没有响应`）已经就位。
- **状态查询无真实数据源**：模型诚实回答或反问，不编造。接设备状态后可注入 `设备状态：客厅灯=开启`（训练过该形态）。
- Laya 微调验收：a2 基线 12/12 领域、回归 11/12、速度 3/3（见开发端 `D:\LlamaFactory\HANDOFF.md`，本包不带训练材料）。板上只做推理。
- 唤醒词在 `kws/my_keywords.txt`（小宸同学 / 小宸小宸 / 你好小宸），改词后重启管线生效。

## 10. 快速故障定位

| 症状 | 查什么 |
|------|--------|
| 模型回复是模板腔（"好的，打开客厅的灯。"） | llama-server 没起或 `BRAIN_URL` 不对；`reply_source` 字段 = "template" 说明大脑没通 |
| 回复 30 秒垃圾音频 | 有人改了 sample_mode 走了 greedy —— 改回 `"fixed"` |
| "场景已切换到Home" 之类英文 | `_scrub_english` 失效或被删 |
| 起服务报 `-fa` 错 | 把 `-fa` 改成 `-fa on` |
| TTS 首句特别慢 | `prompt_codes.json` 丢了或 `MOSS_DIR` 指错 |
| 板子卡死/进程消失 | 内存不够，见坑 8 |
| 意图理解错 | `curl /intent` 看 action/room；别先怀疑 TTS |

---

*完。按第 5 节执行，第 7 节验收，遇到怪现象先对第 8 节。*

## 11. 节点回复播放：先走 PC（2026-10-18 临时方案）

ESP32 节点上 INMP441 占了唯一一路 I2S，喇叭还没接线，所以先让 PC 出声：

1. PC 上跑（保持窗口开着）：
   ```bat
   cd D:\chenvoice-deploy
   python tools\play_reply.py        :: 默认监听 0.0.0.0:8898
   ```
2. 香橙派 `start_server.sh` 里设置：
   ```bash
   export PLAY_BACKEND=http
   export PC_PLAY_URL=http://<PC的局域网地址>:8898     # 例 http://192.168.0.100:8898
   ```
   然后 `scripts/restart_stack.sh`。节点每唤醒一次、管线合成完回复，wav 会 POST 到
   PC 的 `/reply/<文件名>`，PC 用 WMPlayer 串行播放，文件留在 `tools/_pc_replies/`。
3. 验证：PC 上 `curl -X POST --data-binary @某个.wav http://127.0.0.1:8898/reply/t.wav` 应出声。
4. 失败看：PC 播放器没开 → 香橙派 `/tmp/voice_server.log` 里 `[play] http push failed`；
   Windows 防火墙拦 8898 → 放行 Python 的入站。

`PLAY_BACKEND=alsa`（默认）仍走板载 `aplay`，节点喇叭接好后改回来即可。
