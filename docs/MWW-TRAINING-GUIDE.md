# microWakeWord 自训「小宸同学」唤醒词 — 全流程指南

> 目标:在 PC 上训练一个中文唤醒词模型「小宸同学」,部署到 ESP32-S3 (N16R8),
> 替代橙派上的 sherpa-onnx Zipformer KWS,实现每个房间 S3 节点本地唤醒。
>
> **产品口径(用户定):读音沾边就算唤醒** —— 小陈/小程/小城/小呈同学、小宸铜学
> 这些同音/近音词全部当正样本,不训练精细对抗区分。分界线是「这个词的发音形状
> vs 其他一切声音」,不是词与词之间。

---

## 1. 总览

```
① 装环境(PC, conda env)
② 造正样本(piper zh_CN + matcha + 真人录音)          ← 量大,~4000条
③ 造负样本(Common Voice zh_CN + 官方预生成环境包)
④ 增强 + 转频谱特征(mmap 五件套)
⑤ 训练(TF2.15 + RTX 3050) → 流式化 → INT8 量化
⑥ 测指标(FAP/hour ≤1, 唤醒率>90%) → 调参重来
⑦ 打包 .tflite + .json
⑧ ESP32 部署(ESPHome micro_wake_word 组件)
```

| 阶段 | 耗时 | 依赖硬件 |
|---|---|---|
| 环境 | 半天 | 无 |
| 正样本 | 1-2 天 | 无(PC 生成) |
| 负样本 | 1-2 周(顺手攒) | S3/手机录音 |
| 训练调参 | 3-7 天(多数是机器时间) | 无 |
| 部署 | 1 天 | ESP32-S3 |

---

## 2. 环境搭建(Step ①)

### 2.1 磁盘准备 ⚠️ 先做

本机 D 盘仅剩 ~9GB,不够(TF + 数据集 + 校准缓存约 10-15GB)。
先清理 D 盘到 ≥20GB 可用,或整套装到 C 盘(C 盘 30GB 也偏紧)。

### 2.2 conda 环境

microWakeWord 训练栈是 **TensorFlow**(kws_streaming 系 TF 代码),不是 torch。
TF 2.15 不支持本机 base 的 Python 3.14,必须独立环境:

```bash
conda create -n mww python=3.10 -y
conda activate mww

# TensorFlow GPU 版(训练) + 训练器依赖
pip install tensorflow==2.15.1 mmap-ninja audiomentations pyyaml scipy
# GPU 支持库(装在 conda env 内,不动系统驱动;本机驱动 CUDA 12.8 兼容)
conda install -c conda-forge cudatoolkit=12.2 cudnn=8.9 -y

# piper 样本生成器依赖(torch,生成样本时用,非训练)
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
pip install piper-phonemize-cross==1.2.1

# 验证 GPU 可用
python -c "import tensorflow as tf; print(tf.config.list_physical_devices('GPU'))"
```

### 2.3 拉代码

```bash
mkdir -p /d/mww && cd /d/mww
git clone https://gh-proxy.com/https://github.com/kahrendt/microWakeWord
git clone https://gh-proxy.com/https://github.com/rhasspy/piper-sample-generator
# 下载 piper 中文声 zh_CN-huayan-medium (onnx + onnx.json 两个文件)
```

> GitHub 直连不通时全走 gh-proxy.com 前缀(本机已验证可用)。

---

## 3. 正样本(Step ②)—— 大量、多样

**原则:正样本越多越好,覆盖越广越好。** 目标 **~4000 条**,分四路:

| 来源 | 条数 | 生成方式 |
|---|---|---|
| A. piper huayan TTS 变体 | 1500 | 语速/音高/噪声参数扫描 |
| B. matcha-zh-baker TTS 变体 | 800 | 橙派同款声源换着用,防单一 TTS 过拟合 |
| C. 变体唤醒词(见下) | 700 | 小宸小宸/你好小宸/近音词,全部当正样本 |
| D. 真人录音 | 200+ | 你和家人,最贴近真实场景,含金量最高 |

### 3.1 唤醒词清单(全部当正样本)

```
小宸同学   xiǎo chén tóng xué      ← 主词
小陈同学   xiǎo chén tóng xué      (同音)
小程同学   xiǎo chéng tóng xué
小城同学   xiǎo chéng tóng xué
小呈同学   xiǎo chéng tóng xué
小宸铜学   xiǎo chén tóng xué      (近音)
小宸小宸   xiǎo chén xiǎo chén     (橙派 KWS 已注册的变体)
你好小宸   nǐ hǎo xiǎo chén        (橙派 KWS 已注册的变体)
```

### 3.2 piper 批量生成(来源 A/C)

```bash
cd /d/mww/piper-sample-generator
python3 generate_samples.py "xiǎo chén tóng xué" \
  --max-samples 500 --batch-size 64 \
  --noise-scale 0.7 --noise-scale-w 0.8 \
  --length-scales "0.85,1.0,1.15" \
  --output-dir ../samples/pos_xiaochen/
# 对清单里每个词重复;noise-scale 在 0.5~1.0 之间扫
```

听几条确认发音正确再批量(piper 中文 x_low 声质量一般,medium 更好)。

### 3.3 matcha 生成(来源 B)

橙派上的 matcha-zh-baker 模型(`app/tts-model/`)拷到 PC,
PC 装sherpa-onnx 即可离线批量合成:

```python
# gen_matcha_samples.py — sherpa-onnx matcha 批量生成,语速 0.8/0.9/1.0/1.1/1.2
import sherpa_onnx, soundfile as sf, numpy as np
# ... 同橙派 tts.py 的配置,PC 跑 (pip install sherpa-onnx soundfile)
```

### 3.4 真人录音(来源 D,可持续补)

- 手机/橙派/S3 录,16kHz 或 44.1kHz 后续统一降采样
- **场景要真实**:2-3 米距离、侧对麦克风、电视开着、走路时喊
- 每人 50-100 条,每次采集间隔几天再录一批(防同一天声线单一)

---

## 4. 负样本(Step ③)—— 决定误唤醒率

产品口径宽松后,**唯一真正重要的负样本是普通中文语音和环境声**:

| 来源 | 获取方式 | 用途 |
|---|---|---|
| **官方预生成频谱包** | HuggingFace `kahrendt/microwakeword` (speech/music/ambient, 已是 mmap 格式) | 直接下,省 20GB 下载和全套处理 |
| **Common Voice zh_CN** | Mozilla Common Voice 中文子集,下载 zh_CN 剪辑集 | 中文语音误唤醒的**主防线** |
| **家庭实录** | S3/手机录日常:聊天/电视/厨房/安静 | 真实场景,持续攒 |
| FSD50K / FMA / WHAM! | 官方包已含 | 家电/音乐/底噪 |

> 官方文档确认其环境语音负样本含 Common Voice——所以 CV 中文子集与训练管线
> 完全兼容,只是要自己下载处理成频谱。

**量级**:中文语音 20h+ 实录 5-10h 即可起步。

---

## 5. 特征与训练(Step ④⑤)

### 5.1 目录结构(notebook 固定约定)

```
features/
  pos_all/            正样本增强后
    training/  validation/  testing/
  neg_speech_zh/      中文语音
  neg_ambient/        官方环境包
    validation_ambient/  testing_ambient/   ← FAP/hour 从这里出
```

全部经 `mmap_ninja` 转为 ragged mmap 文件夹(notebook cell 7 的流程自动做)。

### 5.2 训练配置 `training_parameters.yaml`

关键项(完整模板 notebook cell 9 会生成,改这些):

```yaml
features:            # 每个目录的 sampling_weight/penalty_weight/truth
  - {features_dir: pos_all,        sampling_weight: 2.0, penalty_weight: 1.0, truth: true}
  - {features_dir: neg_speech_zh,  sampling_weight: 10.0, penalty_weight: 3.0, truth: false}  # 中文负样本加重
  - {features_dir: neg_ambient,    sampling_weight: 5.0, penalty_weight: 1.0, truth: false}
```

### 5.3 训练命令

```bash
conda activate mww && cd /d/mww/microWakeWord
python -m microwakeword.model_train_eval \
  --training_config=../training_parameters.yaml --train 1 \
  --test_tflite_streaming_quantized 1 --use_weights best_weights \
  mixednet \
  --pointwise_filters "64,64,64,64" \
  --repeat_in_block "1,1,1,1" \
  --mixconv_kernel_sizes "[5],[7,11],[9,15],[23]" \
  --residual_connection "0,0,0,0" \
  --first_conv_filters 32 --first_conv_kernel_size 5 --stride 3
```

- RTX 3050 4GB:batch 从默认降到 32-64;一轮 4-12 小时,睡前挂机
- 自动完成:非流式训练 → 转流式 → INT8 量化 → 流式量化版测试
- 断了可续:`--restore_checkpoint 1`

### 5.4 调参速查

| 症状 | 药方 |
|---|---|
| 误唤醒多(FAP 高) | `penalty_weight` 负类 3→5;`probability_cutoff` 提高;补中文负样本 |
| 该响不响 | 正样本多样性(加语速/距离/混响变体);cutoff 降低;`sliding_window_size` 减 |
| 远场唤不醒 | 真人录音补远场条目;训练增强里混响权重加大 |
| 量化后掉点 | 训练步数加;或对敏感层用更高位宽(notebook 不直接支持,进阶) |

---

## 6. 验收指标(Step ⑥)

| 指标 | 目标 | 在哪看 |
|---|---|---|
| FAP/hour(误唤醒/小时) | ≤ 1 | `testing_ambient` 流式量化测试输出 |
| 唤醒率 | >90% | `testing` 正样本召回 |
| 推理延迟 | 无所谓(毫秒级) | S3 上每 30ms 一帧,不构成瓶颈 |

**宽松口径的红利**:不追求词间对抗区分,中文负样本扎实的话 1-2 轮训练即可达标,
预期比标准WakeWord 项目少折腾一半。

---

## 7. 产物与部署(Step ⑦⑧)

### 7.1 产物

```
trained_models/wakeword/tflite_stream_state_internal_quant/
  stream_state_internal_quant.tflite    ← ~100-500KB,这就是模型
```

### 7.2 写模型清单 `xiaochen.json`

照官方 `esphome/micro-wake-word-models/models/v2/okay_nabu.json` 格式:

```json
{
  "type": "micro",
  "wake_word": "hey_xiaochen",
  "author": "HBC",
  "website": "",
  "model": "xiaochen.tflite",
  "version": 1,
  "micro": {
    "probability_cutoff": 0.90,
    "sliding_window_size": 5,
    "feature_step_size": 10,
    "tensor_arena_size": 96456
  }
}
```

- `probability_cutoff` 按训练报告调:宽松口径可放 0.85-0.92(高召回)
- `tensor_arena_size` 看训练日志报的实际值,宁大勿小

### 7.3 ESP32-S3 部署(ESPHome 路线)

```yaml
# ESPHome 配置骨架
i2s_audio:
  - id: i2s_in
    i2s_bclk: GPIO4
    i2s_lrclk: GPIO5
microphone:
  - platform: i2s_audio
    i2s_audio_id: i2s_in
    i2s_din: GPIO6
    adc_type: external
    pdm: false

micro_wake_word:
  models:
    - model: /config/models/xiaochen.json   # 本地清单文件
  microphone: ...
  on_wake_word_detected:
    - then:                                 # 唤醒后动作:通知橙派+开始推流
        - mqtt.publish:
            topic: chenvoice/${room}/wake
            payload: '{"ts": ...}'
        # 推流逻辑(第二波接 AFE/VAD 时细化)

speaker / media_player:                   # MAX98357 播 TTS
  - platform: i2s_audio ...
```

- N16R8 资源:模型 ~500KB Flash + 推理 arena ~100-200KB RAM,充裕
- 第一版半双工:播放 TTS 期间暂停唤醒检测(`micro_wake_word` 的 stop/	start API),
  避免「自己唤醒自己」,不必等 AEC

### 7.4 联调链路(与橙派)

```
S3 唤醒 → MQTT chenvoice/{room}/wake
        → S3 推流 chenvoice/{room}/audio/up (16k/16bit PCM)
橙派    → 现有 SenseVoice ASR → intent → brain → TTS
        → MQTT chenvoice/{room}/tts/down 推 wav
S3      → MAX98357 播放
```

---

## 8. 风险与备忘

1. **D 盘空间**:开工前先清到 ≥20GB
2. **TF 2.15 + cuDNN 版本坑**:conda env 内装,别动系统;不行就 CPU 训练(慢 3-5 倍,一夜也能出)
3. **中文无人区**:公开渠道无中文成功案例可抄,靠指标自判;宽松口径降低了难度
4. **piper 中文声只有 huayan**:多样性靠 matcha + 真人 + 参数扫描补
5. **v1 半双工**:播 TTS 时暂停检测;AEC/全双工是 ESP-SR AFE 的事,第二波再说
6. **数据版权**:Common Voice CC0,FSD50K 系列 CC,家庭实录自家版权,商用无碍

## 9. 与总路线的关系

```
第一波  S3 耳嘴固件(I2S+WiFi+MQTT+按键)          ← 无模型,先行
第二波  本训练流程 → 「小宸同学」上 S3 本地唤醒    ← 本文
第三波  传感器/执行器(温湿度/红外/继电器)
第四波  ESP-DL 自训(事件检测/声纹) —— 同一套方法论
```

本流程跑通一次后,④-⑦ 步就是可复用的生产线:换唤醒词、换数据集,
事件检测/声纹模型走同一条路。
