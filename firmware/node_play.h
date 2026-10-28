// TTS 回放骨架：从香橙派拉 wav，16k/mono/16bit PCM 直接喂给 I2S 扬声器。
//
// 未实现（需要先定硬件/协议，见对话里的两个决策点）：
//   1. wav 头解析（RIFF/chunk 扫描）与格式转换
//   2. 下载进度回调里分块推 I2S，边下边放，避免整段缓冲
//   3. 扬声器 I2S 通道（独立于麦克风 I2S，或复用 + 板上无 AEC 的规避策略）
#pragma once

#include "esphome.h"

inline void cn_node_play(const char *url) {
  ESP_LOGW("cnnode", "playback not implemented yet, url=%s", url);
}
