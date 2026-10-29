# 接线图（Wiring）

四个接线场景：采音节点（ESP32-S3 音频）、对话屏（ST7789 → 香橙派）、ESP-01 刷机、ESP-01 继电器。
采购清单见 [`BOM.md`](BOM.md)。

> 共同铁律：**逻辑器件一律 3.3V**。INMP441 / ESP-01 / ST7789 的 VCC 接 5V 都会烧。CH340 必须拨到 3.3V 档。

---

## 1. ESP32-S3 采音节点（麦克风 + 功放 + 喇叭）

```
                    ┌──────────────────────────────────────────┐
                    │                ESP32-S3                   │
                    │                                          │
   INMP441 麦克风    │  GPIO10 (SD)  ────────────── SD          │
   ┌──────────┐     │  GPIO11 (WS)  ────────────── WS          │
   │ SD  WS   │     │  GPIO12 (SCK) ────────────── SCK         │
   │ SCK L/R  │     │  3V3          ────────────── VDD         │
   │ VDD  GND │     │  GND          ──┬────────── GND          │
   └──────────┘     │                 └────────── L/R → GND    │ ← L/R 接 GND 选左声道
                    │                                          │
   MAX98357 功放     │  GPIO5  (DOUT) ───────────── DIN         │
   ┌──────────┐     │  GPIO6  (BCLK) ───────────── BCLK        │
   │ DIN BCLK │     │  GPIO7  (LRC)  ───────────── LRC         │
   │ LRC VIN  │     │  5V            ───────────── VIN         │
   │ GND SPK+ │     │  GND           ───────────── GND         │
   │ SPK-     │     └──────────────────────────────────────────┘
   └────┬─┬───┘
        │ └── SPK- ──── 喇叭 -
        └──── SPK+ ──── 喇叭 +
```

| ESP32-S3 | 器件引脚 | 说明 |
|----------|---------|------|
| GPIO10 | INMP441 SD | 音频数据 |
| GPIO11 | INMP441 WS | 字选择（LRCLK） |
| GPIO12 | INMP441 SCK | 位时钟（BCLK） |
| GPIO5 | MAX98357 DIN | 音频数据输出 |
| GPIO6 | MAX98357 BCLK | 位时钟 |
| GPIO7 | MAX98357 LRC | 字选择 |
| GPIO2 | 状态 LED（可选） | 唤醒/录音状态指示，串 220Ω 电阻到 GND |

- INMP441 的 **L/R 引脚接 GND**（选左声道），与固件 `channel: left` 对应
- MAX98357 的 VIN 接开发板 5V（USB 供电）；接 3.3V 也能响但音量小
- 固件源码对应：`firmware/xiaochen-node1.yaml`（麦克风 I2S）、`firmware/node_audio.h`（`SPK_DOUT/BCLK/LRC = GPIO5/6/7`）

## 2. 对话屏 ST7789（接香橙派 40-pin 排针，不是接 ESP32）

1.54 寸 ST7789 240×240 SPI 彩屏，走香橙派硬件 SPI3（`/dev/spidev3.0`）。

| 香橙派 40-pin | 引脚功能 | ST7789 | 说明 |
|---------------|---------|--------|------|
| Pin1 | 3.3V | VCC | **只能 3.3V，5V 烧屏** |
| Pin6 | GND | GND | |
| Pin23 | SCLK.3 | SCL | SPI 时钟 |
| Pin19 | MOSI.3 | SDA | SPI 数据 |
| Pin24 | CE0 | CS | 硬件片选（spidev3.0） |
| Pin11 | wPi5 | DC | 数据/命令 |
| Pin13 | wPi7 | RES | 复位（低脉冲） |
| Pin15 | wPi8 | BLK | 背光，高亮；或直接接 Pin17 3.3V 常亮 |
| — | — | MISO | **不接** |

- 驱动服务：`chenvoice-display`（systemd，root 跑，spidev 要权限），代码 `app/st7789_display.py` + `app/dialogue_view.py`
- 颜色反转就把驱动里的 INVON(0x21) 换 INVOFF(0x20)；屏幕倒置改 `MADCTL_ROTATIONS`

## 3. ESP-01 刷机接线（CH340 USB-TTL）

```
   CH340 (必须 3.3V 档)                ESP-01 (8 pin)
   TXD  ──────────────────────────── RXD
   RXD  ──────────────────────────── TXD     ← 交叉
   3.3V ───┬──────────────────────── VCC
           ├──────────────────────── EN(CH_PD)   拉高运行
   GND  ───┴──────────────────────── GND
              IO0 ─── 刷机时接 GND（下载模式），运行时断开
```

- **VCC 只接 3.3V**，5V 烧模块；EN(CH_PD) 必须拉高否则串口全静默
- CH340 的 3.3V 带不动 WiFi——刷完固件换外部供电测试
- ROM 引导不应答（乱按复位无 `rst:0x1` 输出）= 没电 / 晶振坏 / TX 断——同线材换好板秒通即可定位

## 4. ESP-01 ↔ 继电器板

ESP-01 插一体继电器底板（底板上 8-pin 插座 + 继电器模块），信号脚是 GPIO0：

| ESP-01 | 继电器板 | 说明 |
|--------|---------|------|
| VCC / GND | 供电 | 3.3V 逻辑；继电器线圈侧按底板要求供电 |
| GPIO0 | IN（信号） | Tasmota 模板 `GPIO[0]=256`（Relay_i，低电平触发反相） |

Tasmota 模板（继电器反相，灯亮灭才正确）：

```json
{"NAME":"ESP01-Relay","GPIO":[256,0,0,0,0,0,0,0],"FLAG":0,"BASE":18}
```

> 坑：继电器代码是 **256（Relay_i）/ 224（Relay）**，不是老文档的 21。写 21 能保存但不建继电器通道（查代码表：`cmnd/<topic>/GPIOs`）。
