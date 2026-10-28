# -*- coding: utf-8 -*-
"""Re-apply mwwprobe counter instrumentation to micro_wake_word.cpp.

Codegen copies pristine component sources into build/src and wipes this patch.
build_node1.sh re-copies the saved patched file after codegen; this script
rebuilds that saved file from pristine + the final counter patches.
"""
import sys

BUILD_CPP = r"D:\esphome-xiaochen\.esphome\build\xiaochen-node1\src\esphome\components\micro_wake_word\micro_wake_word.cpp"
SAVED_CPP = r"D:\esphome-xiaochen\micro_wake_word.cpp.mwwprobe"
TARGETS = [BUILD_CPP, SAVED_CPP]

DECL_OLD = """#ifdef USE_OTA
#include "esphome/components/ota/ota_backend.h"
#endif"""
DECL_NEW = DECL_OLD + """

// mwwprobe counter declarations (definitions live further down)
extern "C" {
extern uint32_t g_mww_probe_cb, g_mww_probe_cb_drop, g_mww_probe_loop;
extern uint32_t g_mww_probe_starve, g_mww_probe_feat;
extern uint32_t g_mww_probe_avail, g_mww_probe_use, g_mww_probe_st;
}"""

CB_OLD = """  this->microphone_source_->add_data_callback([this](const std::vector<uint8_t> &data) {
    if (this->state_ == State::STOPPED) {
      return;
    }
    std::shared_ptr<RingBuffer> temp_ring_buffer = this->ring_buffer_.lock();
    if (this->ring_buffer_.use_count() > 1) {
      size_t bytes_free = temp_ring_buffer->free();

      if (bytes_free < data.size()) {
        xEventGroupSetBits(this->event_group_, EventGroupBits::WARNING_FULL_RING_BUFFER);
        temp_ring_buffer->reset();
      }
      temp_ring_buffer->write((void *) data.data(), data.size());
    }
  });"""
CB_NEW = """  this->microphone_source_->add_data_callback([this](const std::vector<uint8_t> &data) {
    if (this->state_ == State::STOPPED) {
      g_mww_probe_cb_drop++;
      g_mww_probe_st = (uint32_t) this->state_;
      return;
    }
    std::shared_ptr<RingBuffer> temp_ring_buffer = this->ring_buffer_.lock();
    if (this->ring_buffer_.use_count() > 1) {
      size_t bytes_free = temp_ring_buffer->free();

      if (bytes_free < data.size()) {
        xEventGroupSetBits(this->event_group_, EventGroupBits::WARNING_FULL_RING_BUFFER);
        temp_ring_buffer->reset();
      }
      temp_ring_buffer->write((void *) data.data(), data.size());
      g_mww_probe_cb++;
      g_mww_probe_use = (uint32_t) this->ring_buffer_.use_count();
      g_mww_probe_st = (uint32_t) this->state_;
    } else {
      g_mww_probe_cb_drop++;
      g_mww_probe_use = (uint32_t) this->ring_buffer_.use_count();
      g_mww_probe_st = (uint32_t) this->state_;
    }
  });"""

DEFS_OLD = "void MicroWakeWord::add_wake_word_model(WakeWordModel *model) { this->wake_word_models_.push_back(model); }"
DEFS_NEW = DEFS_OLD + """

// mwwprobe counters - printed by chenvoice::dump_clean (task-context ESP_LOGI never reaches the log transport)
extern "C" {
uint32_t g_mww_probe_cb = 0, g_mww_probe_cb_drop = 0, g_mww_probe_loop = 0;
uint32_t g_mww_probe_starve = 0, g_mww_probe_feat = 0;
uint32_t g_mww_probe_avail = 0, g_mww_probe_use = 0, g_mww_probe_st = 0;
}"""

LOOP_OLD = """      while (!(xEventGroupGetBits(this_mww->event_group_) & COMMAND_STOP)) {
        audio_buffer->transfer_data_from_source(pdMS_TO_TICKS(DATA_TIMEOUT_MS));

        if (audio_buffer->available() < new_bytes_to_process) {
          // Insufficient data to generate new spectrogram features, read more next iteration
          continue;
        }"""
LOOP_NEW = """      while (!(xEventGroupGetBits(this_mww->event_group_) & COMMAND_STOP)) {
        g_mww_probe_loop++;
        audio_buffer->transfer_data_from_source(pdMS_TO_TICKS(DATA_TIMEOUT_MS));
        g_mww_probe_avail = (uint32_t) audio_buffer->available();

        if (audio_buffer->available() < new_bytes_to_process) {
          // Insufficient data to generate new spectrogram features, read more next iteration
          g_mww_probe_starve++;
          continue;
        }
        g_mww_probe_feat++;"""

PAIRS = [(DECL_OLD, DECL_NEW), (CB_OLD, CB_NEW), (DEFS_OLD, DEFS_NEW), (LOOP_OLD, LOOP_NEW)]


def patch(text):
    for old, new in PAIRS:
        n = text.count(old)
        if n != 1:
            raise SystemExit("anchor count %d != 1 for: %r" % (n, old[:60]))
        text = text.replace(old, new, 1)
    return text


def main():
    src = open(BUILD_CPP, encoding="utf8", newline="").read()
    if "g_mww_probe_cb" in src:
        raise SystemExit("build tree already patched - abort to avoid double-patch")
    patched = patch(src)
    for path in TARGETS:
        with open(path, "w", encoding="utf8", newline="") as f:
            f.write(patched)
        print("[patched]", path)


if __name__ == "__main__":
    main()
