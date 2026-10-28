// 唤醒后录音的接线层（由 yaml 的 lambda 调用 chenvoice::listen()）。
//
// 音频只有一个读者：micro_wake_word 自己的 mic_task 读 I2S，并把每帧数据交给
// 注册在 mic 上的回调。yaml 的 microphone.on_data 注册 cn_sample_cb，把同一份
// 数据（32bit 槽取高 16 位）存进干净环形缓存。录音、统计、导出全部从这份缓存
// 取数，不再自己读 I2S——两个读者会抢同一路 DMA。
#pragma once

#include "esphome.h"
#include "esphome/components/audio/audio_transfer_buffer.h"
#include "esphome/components/i2s_audio/microphone/i2s_audio_microphone.h"
#include "esphome/components/micro_wake_word/micro_wake_word.h"
#include <esp_heap_caps.h>
#include <driver/i2s_std.h>
#include <esp_http_client.h>
#include <esp_http_server.h>
#include <lwip/sockets.h>
#include <lwip/dns.h>
#include <netdb.h>
#include <mbedtls/base64.h>
#include <cmath>
#include <cstring>

// mwwprobe counters (defined in micro_wake_word.cpp)
extern "C" {
extern uint32_t g_mww_probe_cb, g_mww_probe_cb_drop, g_mww_probe_loop;
extern uint32_t g_mww_probe_starve, g_mww_probe_feat;
extern uint32_t g_mww_probe_avail, g_mww_probe_use, g_mww_probe_st;
}

namespace chenvoice {

static const char *const TAG = "cnnode";

// 当前唤醒周期是否还在处理（诊断/录音互斥）。
volatile bool g_listening = false;
volatile int16_t g_peak = 0;
// 自启动起累计写入的样本数：录音按"唤醒之后新增的部分"截取。
volatile uint64_t g_cb_samples = 0;
static char s_cb_task[24] = {0};
#define UPLOAD_GUARD_MS 60000u
static uint32_t s_listen_deadline = 0;

// 干净音频：16k mono、满量程（mww 实际看到的那一路）。
#define CLEAN_RATE 16000
#define CLEAN_SECONDS 12
static int16_t *g_clean = nullptr;
static size_t g_clean_w = 0, g_clean_len = 0;
static portMUX_TYPE g_clean_mux = portMUX_INITIALIZER_UNLOCKED;

inline size_t clean_tail(int16_t *dst, size_t n) {
  const size_t ring = CLEAN_RATE * CLEAN_SECONDS;
  portENTER_CRITICAL(&g_clean_mux);
  if (n > g_clean_len) n = g_clean_len;
  size_t start = (g_clean_w + ring - n) % ring;
  for (size_t i = 0; i < n; i++) dst[i] = g_clean[(start + i) % ring];
  portEXIT_CRITICAL(&g_clean_mux);
  return n;
}

// 录音缓冲：PSRAM 里的裸块，std::vector 在内部堆上分配 192KB 会直接 abort。
struct RecBuffer {
  int16_t *data;
  size_t len;
};

// 正在导出的那段录音（listen.wav）。
static int16_t *g_snap = nullptr;
static size_t g_snap_len = 0, g_snap_cap = 0;
static volatile bool s_busy = false;

inline void mww_start_owned() { id(micro_wake_word_).start(); }
inline void mww_stop_owned() { id(micro_wake_word_).stop(); }

// mww 停止时 MicrophoneSource::stop() 会顺手把 mic 也停了（mic_task 断流），
// 录音就采不到新样本。占一个 listener 槽把 mic 撑住：槽计数 < MAX 才停 mic。
inline void mic_hold() { id(mic_in).start(); }
inline void mic_release() { id(mic_in).stop(); }
static void spk_play_scaled(const int16_t *pcm, size_t n);
static void spk_write(const int16_t *pcm, size_t n);  // 定义在喇叭段，应答音前置用

// ---- 唤醒应答音「我在」----
// 板上内置 PCM（16k mono），唤醒确认后先播再录：让用户知道开始听了，也避免
// 用户抢在录音窗口前开口。播的时候 mww 已停，无 AEC 也不会自触发。
#include "wozai_pcm.h"
static void play_wozai() {
  spk_write(kWoZai, kWoZaiLen);
}

// ---- HTTP 文件导出（/fn/<名字>）----
// 板子跑着唤醒检测没法边听边自己放，把缓存里的音频导出成 WAV 拉到 PC 上听，
// 也用来拿"真唤醒词"录音离线重跑模型。两个槽位轮换，单文件上限 1MB。
#define FN_PORT 9890
#define FN_SLOT_BYTES (1024 * 1024)
static uint8_t *g_fn[2] = {nullptr, nullptr};
static size_t g_fn_len = 0;
static int g_fn_cur = -1;
static char g_fn_name[2][24];
static httpd_handle_t g_fn_srv = nullptr;

inline void fn_init() {
  g_fn[0] = (uint8_t *) heap_caps_malloc(FN_SLOT_BYTES, MALLOC_CAP_SPIRAM);
  g_fn[1] = (uint8_t *) heap_caps_malloc(FN_SLOT_BYTES, MALLOC_CAP_SPIRAM);
  if (g_fn[0] == nullptr || g_fn[1] == nullptr) ESP_LOGE(TAG, "file slots alloc failed");
}

inline void fn_begin(const char *name) {
  int nxt = (g_fn_cur + 1) % 2;
  if (g_fn[nxt] == nullptr) return;
  portENTER_CRITICAL(&g_clean_mux);
  g_fn_cur = nxt;
  strncpy(g_fn_name[nxt], name, sizeof(g_fn_name[nxt]) - 1);
  g_fn_name[nxt][sizeof(g_fn_name[nxt]) - 1] = 0;
  g_fn_len = 0;
  portEXIT_CRITICAL(&g_clean_mux);
}

// 44 字节 WAV 头 + 16bit mono PCM，写进当前槽位。
inline void fn_write_wav(const int16_t *pcm, size_t samples, uint32_t sr) {
  if (g_fn_cur < 0) return;
  uint8_t *p = g_fn[g_fn_cur];
  const uint32_t db = samples * 2;
  if (db + 44 > FN_SLOT_BYTES) return;
  auto w32 = [](uint8_t *d, uint32_t v) {
    d[0] = v;
    d[1] = v >> 8;
    d[2] = v >> 16;
    d[3] = v >> 24;
  };
  auto w16 = [](uint8_t *d, uint16_t v) {
    d[0] = v;
    d[1] = v >> 8;
  };
  memcpy(p, "RIFF", 4);
  w32(p + 4, 36 + db);
  memcpy(p + 8, "WAVEfmt ", 8);
  w32(p + 16, 16);
  w16(p + 20, 1);
  w16(p + 22, 1);
  w32(p + 24, sr);
  w32(p + 28, sr * 2);
  w16(p + 32, 2);
  w16(p + 34, 16);
  memcpy(p + 36, "data", 4);
  w32(p + 40, db);
  memcpy(p + 44, pcm, db);
  portENTER_CRITICAL(&g_clean_mux);
  g_fn_len = 44 + db;
  portEXIT_CRITICAL(&g_clean_mux);
}

static esp_err_t fn_get(httpd_req_t *req) {
  ESP_LOGI(TAG, "fn_get uri=%s", req->uri);
  const char *name = req->uri + 4;  // "/fn/"
  int slot = -1;
  portENTER_CRITICAL(&g_clean_mux);
  for (int i = 0; i < 2; i++) {
    if (g_fn_len && strcmp(name, g_fn_name[i]) == 0) {
      slot = i;
      break;
    }
  }
  if (slot < 0) {
    portEXIT_CRITICAL(&g_clean_mux);
    httpd_resp_set_status(req, "404 Not Found");
    httpd_resp_set_type(req, "text/plain");
    httpd_resp_send(req, "no such file", HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
  }
  size_t len = g_fn_len;
  const char *type = (len > 4 && memcmp(g_fn[slot], "RIFF", 4) == 0) ? "audio/wav" : "text/plain";
  httpd_resp_set_type(req, type);
  portEXIT_CRITICAL(&g_clean_mux);
  // 两个槽轮换写，发送期间不会被同槽覆盖。发送会阻塞（分包、等 ACK），
  // 绝不能夹在临界区里。
  return httpd_resp_send(req, (const char *) g_fn[slot], len);
}

inline void fn_server_start() {
  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.server_port = FN_PORT;
  cfg.ctrl_port = FN_PORT + 1;
  cfg.max_uri_handlers = 2;
  cfg.lru_purge_enable = true;
  cfg.stack_size = 8192;  // 默认 4KB 在 httpd_resp_send 里会溢出
  // 默认配置宏里 uri_match_fn 是 NULL（整串精确匹配），通配要显式设。
  cfg.uri_match_fn = httpd_uri_match_wildcard;
  if (httpd_start(&g_fn_srv, &cfg) != ESP_OK) {
    ESP_LOGE(TAG, "httpd start failed");
    return;
  }
  // 处理器表是成员变量，注册时不能传栈上的临时对象（释放后匹配阶段就崩）。
  httpd_uri_t *u = (httpd_uri_t *) heap_caps_malloc(sizeof(httpd_uri_t), MALLOC_CAP_INTERNAL);
  u->uri = "/fn/*";
  u->method = (httpd_method_t) HTTP_ANY;
  u->handler = fn_get;
  u->user_ctx = nullptr;
  ESP_LOGI(TAG, "file server on :%d (rc=%d)", FN_PORT, (int) httpd_register_uri_handler(g_fn_srv, u));
}

// ---- 音频统计（mww 口径）----
// 缓存里就是 mww 送进特征提取的那一路。语音的 RMS 一般占满量程 5%~20%；
// 只有尖峰、RMS 极低（<1%）就是电平/格式有问题。
inline void analyze(const int16_t *pcm, size_t n, const char *tag) {
  int32_t peak = 0;
  int64_t sum_sq = 0;
  size_t blocks = 0, loud = 0, clipped = 0;
  for (size_t i = 0; i < n; i += 1600) {
    size_t m = n - i < 1600 ? n - i : 1600;
    int32_t blk = 0;
    for (size_t j = 0; j < m; j++) {
      int32_t v = pcm[i + j];
      int32_t a = v < 0 ? -v : v;
      if (a > blk) blk = a;
      if (a > peak) peak = a;
      sum_sq += (int64_t) v * v;
      if (a >= 32000) clipped++;
    }
    blocks++;
    if (blk > 900) loud++;
  }
  double rms = n ? sqrt((double) sum_sq / n) : 0;
  ESP_LOGI(TAG, "STAT %s len=%.2fs peak=%d(%d%%FS) rms=%.0f(%.2f%%FS) loud_blocks=%u/%u clipped=%u", tag,
           n / 16000.0, (int) peak, (int) (peak / 327.68), rms, rms / 327.68, (unsigned) loud, (unsigned) blocks,
           (unsigned) clipped);
}

// ---- 录音 ----
struct ListenParams {
  int max_sec;
  int silence_ms;
};

// 等静音收尾（silence_ms）或录满 max_sec，从缓存里截最近一段。
// base_s0：样本基线（应答音之后的起点）；0 = 旧行为（最近 0.5s 起）。
inline size_t record(RecBuffer &buf, const ListenParams &p, int16_t &peak, uint64_t base_s0 = 0) {
  buf.data = nullptr;
  buf.len = 0;
  const size_t cap = size_t(CLEAN_RATE) * p.max_sec;
  int16_t *mem = (int16_t *) heap_caps_malloc(cap * sizeof(int16_t), MALLOC_CAP_SPIRAM);
  buf.data = mem;
  if (mem == nullptr) {
    ESP_LOGE(TAG, "psram alloc %u failed", (unsigned) (cap * sizeof(int16_t)));
    return 0;
  }
  // 唤醒声就在缓存末尾（0.5s 指示音 + "小宸同学" ≈ 1.5s）：从这里开始录，别被
  // mww 剩下的检测延时吃掉；唤醒前 0.5s 也保留一点前导。
  const uint64_t s0 = base_s0 != 0 ? base_s0
                                   : (g_cb_samples > 8000 ? g_cb_samples - 8000 : 0);
  const uint32_t start = millis();
  uint32_t last_loud = start;
  int16_t last_peak = 0;
  bool heard = false;  // 有人开口（blk>300）才起算静音收尾，容忍喊完「小宸同学」后的犹豫
  while (millis() - start < (uint32_t) (p.max_sec * 1000 + 1000)) {
    static int16_t probe[1600];  // 100ms
    size_t n = clean_tail(probe, 1600);
    int16_t blk = 0;
    for (size_t i = 0; i < n; i++) {
      int16_t a = probe[i] < 0 ? (int16_t) -probe[i] : probe[i];
      if (a > blk) blk = a;
    }
    if (blk > last_peak) last_peak = blk;
    if (blk > 300) {
      last_loud = millis();
      heard = true;
    }
    vTaskDelay(pdMS_TO_TICKS(100));
    bool full = millis() - start >= (uint32_t) p.max_sec * 1000;
    // 没等到开口：2.5s 放弃（假唤醒快速归位，别把真唤醒挤掉）。
    bool gave_up = !heard && millis() - start > 2500;
    bool quiet = heard && p.silence_ms > 0 && millis() - last_loud > (uint32_t) p.silence_ms &&
                 millis() - start > 500;
    if (full || quiet || gave_up) break;
  }
  peak = last_peak;
  uint64_t got = g_cb_samples - s0;
  size_t n = clean_tail(mem, got < cap ? (size_t) got : cap);
  ESP_LOGI(TAG, "captured %u new samples (%.2fs)", (unsigned) got, got / 16000.0);
  buf.len = n;
  // 同一段留一份给导出（PSRAM 有余量才留）。
  if (heap_caps_get_free_size(MALLOC_CAP_SPIRAM) > n * 2 + 512 * 1024) {
    if (n > g_snap_cap) {
      heap_caps_free(g_snap);
      g_snap = (int16_t *) heap_caps_malloc(n * 2, MALLOC_CAP_SPIRAM);
      g_snap_cap = g_snap != nullptr ? n : 0;
    }
    if (g_snap != nullptr) {
      memcpy(g_snap, mem, n * 2);
      g_snap_len = n;
    }
  }
  return n;
}

// POST /node/audio {device,sr,format,audio(b64 s16le)}，200 时取回 reply_text / reply_url。
inline bool upload(const RecBuffer &buf, std::string &reply, std::string &url, int &status_out) {
  const size_t n = buf.len * 2;
  std::string head = "{\"device\":\"node1\",\"sr\":16000,\"format\":\"s16le\",\"audio\":\"";
  size_t b64_cap = 4 * ((n + 2) / 3) + 4;
  uint8_t *b64 = (uint8_t *) heap_caps_malloc(b64_cap, MALLOC_CAP_SPIRAM);
  uint8_t *body = (uint8_t *) heap_caps_malloc(head.size() + b64_cap + 4, MALLOC_CAP_SPIRAM);
  if (b64 == nullptr || body == nullptr) {
    ESP_LOGE(TAG, "psram alloc for upload failed");
    heap_caps_free(b64);
    heap_caps_free(body);
    return false;
  }
  size_t b64_len = 0;
  if (mbedtls_base64_encode(b64, b64_cap, &b64_len, reinterpret_cast<const uint8_t *>(buf.data), n) != 0) {
    ESP_LOGE(TAG, "base64 encode failed");
    heap_caps_free(b64);
    heap_caps_free(body);
    return false;
  }
  size_t body_len = head.size();
  memcpy(body, head.data(), body_len);
  memcpy(body + body_len, b64, b64_len);
  body_len += b64_len;
  memcpy(body + body_len, "\"}", 2);
  body_len += 2;
  heap_caps_free(b64);

  esp_http_client_config_t cfg{};
  static const char *URL = "http://192.168.207.150:8899/node/audio";
  cfg.url = URL;
  cfg.method = HTTP_METHOD_POST;
  cfg.timeout_ms = 60000;
  esp_http_client_handle_t h = esp_http_client_init(&cfg);
  esp_http_client_set_header(h, "Content-Type", "application/json");
  esp_http_client_set_post_field(h, reinterpret_cast<const char *>(body), body_len);
  esp_err_t err = esp_http_client_perform(h);
  status_out = esp_http_client_get_status_code(h);
  bool ok = err == ESP_OK && status_out == 200;
  if (ok) {
    std::string resp;
    char chunk[256];
    int r;
    while ((r = esp_http_client_read(h, chunk, sizeof(chunk))) > 0) {
      resp.append((const char *) chunk, r);
      if (resp.size() > 8192) break;
    }
    auto grab = [&](const std::string &key, std::string &out) {
      size_t at = resp.find(key);
      if (at == std::string::npos) return;
      size_t s = at + key.size(), e = resp.find('"', s);
      if (e != std::string::npos && e > s) out = resp.substr(s, e - s);
    };
    grab("\"reply_text\":\"", reply);
    grab("\"reply_url\":\"", url);
  }
  esp_http_client_cleanup(h);
  return ok;
}

// 唤醒瞬间告知香橙派（屏显立刻变「聆听中」）。独立小任务发，失败不阻塞录音；
// 服务端状态带 TTL，丢包自己回「待机」。
inline void wake_ping_task(void *) {
  static const char body[] = "{\"device\":\"node1\"}";
  esp_http_client_config_t cfg{};
  static const char *URL = "http://192.168.207.150:8899/node/wake";
  cfg.url = URL;
  cfg.method = HTTP_METHOD_POST;
  cfg.timeout_ms = 1500;
  esp_http_client_handle_t h = esp_http_client_init(&cfg);
  esp_http_client_set_header(h, "Content-Type", "application/json");
  esp_http_client_set_post_field(h, body, strlen(body));
  esp_err_t err = esp_http_client_perform(h);
  ESP_LOGI(TAG, "wake ping rc=%d status=%d", (int) err, esp_http_client_get_status_code(h));
  esp_http_client_cleanup(h);
  vTaskDelete(nullptr);
}
inline void wake_ping() { xTaskCreate(wake_ping_task, "wake_ping", 3072, nullptr, 5, nullptr); }

// 录音后导出一份 WAV（listen.wav）：PC 上取 http://<node>:9890/fn/listen.wav
inline void export_task(void *) {
  if (g_snap != nullptr && g_snap_len > 0) {
    fn_begin("listen.wav");
    fn_write_wav(g_snap, g_snap_len, CLEAN_RATE);
    ESP_LOGI(TAG, "export listen.wav (%.2fs) WAVB:listen.wav", g_snap_len / 16000.0);
  }
  vTaskDelete(nullptr);
}

inline void listen_task(void *arg) {
  auto *p = (ListenParams *) arg;
  // 先应答「我在」再录音：录音窗口从应答音结束算起，用户不会抢跑。
  play_wozai();
  // 应答音混进环形缓存了；把"新增样本"基线推进到应答音刚结束的位置，
  // record() 从这里开始截，录到的纯是命令。
  const uint64_t after_ack = g_cb_samples;  // play 阻塞返回时，播出的样本已全部被回调收进缓存
  vTaskDelay(pdMS_TO_TICKS(50));            // 再留 50ms 缓冲，避开应答音尾沿
  RecBuffer buf;
  int16_t peak = 0;
  size_t n = record(buf, *p, peak, after_ack);
  g_peak = peak;
  ESP_LOGW("TCHK", "cb_task=%s rec_task=%s samples=%u", s_cb_task[0] ? s_cb_task : "?", pcTaskGetName(nullptr), (unsigned) g_cb_samples);
  ESP_LOGI(TAG, "record: n=%u data=%p snap=%p/%u", (unsigned) n, (void *) buf.data, (void *) g_snap, (unsigned) g_snap_len);
  analyze(buf.data, n, "record");
  ESP_LOGI(TAG, "recorded %.2fs peak=%d", n / 16000.0, peak);
  std::string reply, url;
  int status = 0;
  bool ok = n >= 4800 && upload(buf, reply, url, status);
  heap_caps_free(buf.data);
  ESP_LOGI(TAG, "upload %s status=%d url=%s", ok ? "ok" : "FAIL", status, url.c_str());
  delete p;
  // 无 AEC，本轮回复不在板上放音；上传完直接恢复唤醒检测（MQTT 断了也不至于哑掉）。
  g_listening = false;
  s_listen_deadline = 0;
  mww_start_owned();  // mww 先接回槽
  mic_release();      // 再还我们的撑槽，mic 不会闪断
  if (g_snap != nullptr && g_snap_len > 0) {
    xTaskCreateWithCaps(export_task, "cn_export", 8192, nullptr, 1, nullptr,
                        MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  }
  vTaskDelete(nullptr);
}

// 唤醒后调用：录 max_sec（静音 silence_ms 提前收尾）→ 上传香橙派 → 恢复唤醒检测。
inline void listen(int max_sec = 6, int silence_ms = 800) {
  ESP_LOGI(TAG, "listen: enter max=%d silence=%d listening=%d", max_sec, silence_ms, (int) g_listening);
  if (g_listening) {
    ESP_LOGW(TAG, "listen already running, ignored");
    return;
  }
  g_listening = true;
  s_listen_deadline = millis() + UPLOAD_GUARD_MS;
  mic_hold();  // mww 停了 mic 也会停，撑住才能采到命令
  auto *params = new ListenParams{max_sec, silence_ms};
  if (xTaskCreateWithCaps(listen_task, "cn_listen", 12288, params, 2, nullptr,
                          MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT) != pdPASS) {
    ESP_LOGE(TAG, "listen: task create FAILED (internal heap low?)");
    delete params;
    mic_release();
    g_listening = false;
    s_listen_deadline = 0;
    mww_start_owned();
  }
}

// ---- 喇叭（MAX98357）----
// 抄 xiaozhi-esp32 的 NoAudioCodec::Write：不用 ESPHome speaker 组件（它的
// 状态机在主循环被占用时永远走不到 RUNNING），直接持有独立 I2S TX 通道，
// i2s_channel_write 阻塞写 DMA。32bit 槽 / mono / 左槽，和 ESPHome 的
// i2s_spk（GPIO6/7/5）互不冲突——yaml 里那个 speaker 已不使用。
#define SPK_BCLK GPIO_NUM_6
#define SPK_LRC GPIO_NUM_7
#define SPK_DOUT GPIO_NUM_5
#define SPK_DMA_DESC_NUM 6
#define SPK_DMA_FRAME_NUM 240
static i2s_chan_handle_t s_spk_tx = nullptr;

inline bool spk_i2s_init() {
  if (s_spk_tx != nullptr) return true;
  i2s_chan_config_t chan_cfg = {};
  chan_cfg.id = I2S_NUM_1;  // 麦克风在 ESPHome 那边占 0
  chan_cfg.role = I2S_ROLE_MASTER;
  chan_cfg.dma_desc_num = SPK_DMA_DESC_NUM;
  chan_cfg.dma_frame_num = SPK_DMA_FRAME_NUM;
  chan_cfg.auto_clear_after_cb = true;
  if (i2s_new_channel(&chan_cfg, &s_spk_tx, nullptr) != ESP_OK) {
    ESP_LOGE(TAG, "spk: i2s_new_channel failed");
    return false;
  }
  i2s_std_config_t std_cfg = {};
  std_cfg.clk_cfg.sample_rate_hz = CLEAN_RATE;
  std_cfg.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_256;
  // Zero-init leaves clk_src invalid (sclk reads 0 -> "clock calculate failed").
  std_cfg.clk_cfg.clk_src = I2S_CLK_SRC_DEFAULT;
  std_cfg.slot_cfg.data_bit_width = I2S_DATA_BIT_WIDTH_32BIT;
  std_cfg.slot_cfg.slot_bit_width = I2S_SLOT_BIT_WIDTH_AUTO;
  std_cfg.slot_cfg.slot_mode = I2S_SLOT_MODE_MONO;
  std_cfg.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;
  std_cfg.slot_cfg.ws_width = I2S_DATA_BIT_WIDTH_32BIT;
  std_cfg.gpio_cfg.bclk = SPK_BCLK;
  std_cfg.gpio_cfg.ws = SPK_LRC;
  std_cfg.gpio_cfg.dout = SPK_DOUT;
  std_cfg.gpio_cfg.mclk = I2S_GPIO_UNUSED;
  std_cfg.gpio_cfg.din = I2S_GPIO_UNUSED;
  esp_err_t err = i2s_channel_init_std_mode(s_spk_tx, &std_cfg);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "spk: init_std_mode failed err=%d", (int) err);
    return false;
  }
  err = i2s_channel_enable(s_spk_tx);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "spk: enable failed err=%d", (int) err);
    return false;
  }
  ESP_LOGI(TAG, "spk: i2s ready (32bit mono %dHz)", CLEAN_RATE);
  return true;
}

// 小智同款写法：int16 → int32（左对齐到 32bit 槽），阻塞写 DMA。
// MAX98357 的 SD 接 3.3V 常开，无需使能脚。
static void spk_write(const int16_t *pcm, size_t n) {
  if (!spk_i2s_init()) return;
  constexpr size_t CHUNK = SPK_DMA_FRAME_NUM * 4;  // 960 样本/块
  static int32_t buf[CHUNK];
  for (size_t i = 0; i < n; i += CHUNK) {
    size_t m = n - i < CHUNK ? n - i : CHUNK;
    for (size_t j = 0; j < m; j++) {
      // 样本左对齐进 32bit 槽：乘 65536 后 MAX98357 取高 16bit
      int32_t v = (int32_t) pcm[i + j] * (1 << 16);
      buf[j] = v;
    }
    size_t bytes = m * sizeof(int32_t);
    size_t written = 0;
    if (i2s_channel_write(s_spk_tx, buf, bytes, &written, portMAX_DELAY) != ESP_OK) {
      ESP_LOGE(TAG, "spk: i2s write failed");
      return;
    }
  }
}

// 兼容旧调用：gain 版本（diag 用 2 倍放大听清微弱录音）
static void spk_play_scaled(const int16_t *pcm, size_t n) {
  spk_i2s_init();
  static std::vector<int16_t> tmp;
  tmp.resize(n);
  for (size_t i = 0; i < n; i++) {
    int32_t v = pcm[i] * 2;
    tmp[i] = v > 32767 ? 32767 : (v < -32768 ? -32768 : (int16_t) v);
  }
  spk_write(tmp.data(), n);
  ESP_LOGI(TAG, "spk: wrote %u samples (scaled)", (unsigned) n);
}

// 原样播放（不放大）：缓存里的录音已经是满量程，再乘 2 只会削顶。
static void spk_play_raw(const int16_t *pcm, size_t n) {
  spk_write(pcm, n);
  ESP_LOGI(TAG, "spk: wrote %u samples", (unsigned) n);
}

// ---- 放 PC 上的 WAV：MQTT chenvoice/control/node1/play <url> ----
// 只收 16k/8k mono 16bit PCM WAV（播放可按 8k↔16k 重采样）。数据先整段进 PSRAM
// （限 600s@8k），边下边播会断音。
static size_t wav_data_offset(const uint8_t *h, size_t n) {
  if (n < 12 || memcmp(h, "RIFF", 4) != 0 || memcmp(h + 8, "WAVE", 4) != 0) return 0;
  size_t i = 12;
  while (i + 8 <= n) {
    uint32_t sz = (uint32_t) h[i + 4] | ((uint32_t) h[i + 5] << 8) | ((uint32_t) h[i + 6] << 16) |
                  ((uint32_t) h[i + 7] << 24);
    if (memcmp(h + i, "data", 4) == 0) return i + 8;
    i += 8 + sz + (sz & 1);
  }
  return 0;
}

inline void play_task(void *arg) {
  std::string *url_p = (std::string *) arg;
  std::string url = *url_p;
  delete url_p;
  // esp_http_client 在这个工程里 connect/read 会卡死（原因没查清，疑似和 mww/
  // speaker 的任务交互有关），换成裸 lwIP socket + recv 超时，行为可控。
  // 只支持 http://host:port/path。
  ESP_LOGI(TAG, "play: GET %s", url.c_str());
  int16_t *dst = nullptr;
  size_t cap = 0, len = 0;
  int sock = -1;
  do {
    if (url.rfind("http://", 0) != 0) {
      ESP_LOGE(TAG, "play: only http:// supported");
      break;
    }
    std::string rest = url.substr(7);
    size_t slash = rest.find('/');
    if (slash == std::string::npos) {
      ESP_LOGE(TAG, "play: no path in url");
      break;
    }
    std::string hostport = rest.substr(0, slash), path = rest.substr(slash);
    std::string host = hostport;
    uint16_t port = 80;
    size_t colon = hostport.rfind(':');
    if (colon != std::string::npos) {
      host = hostport.substr(0, colon);
      port = (uint16_t) atoi(hostport.c_str() + colon + 1);
    }
    struct sockaddr_in sa{};
    sa.sin_family = AF_INET;
    sa.sin_port = htons(port);
    if (inet_pton(AF_INET, host.c_str(), &sa.sin_addr) != 1) {
      // 域名解析（含 mDNS .local）
      struct addrinfo hint{}; hint.ai_family = AF_INET;
      struct addrinfo *res = nullptr;
      if (getaddrinfo(host.c_str(), nullptr, &hint, &res) != 0 || res == nullptr) {
        ESP_LOGE(TAG, "play: resolve %s failed", host.c_str());
        break;
      }
      sa.sin_addr = ((struct sockaddr_in *) res->ai_addr)->sin_addr;
      freeaddrinfo(res);
    }
    sock = ::socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) {
      ESP_LOGE(TAG, "play: socket failed");
      break;
    }
    // 收发各 10s 超时：绝不无限等
    struct timeval tv{10, 0};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    if (connect(sock, (struct sockaddr *) &sa, sizeof(sa)) != 0) {
      ESP_LOGE(TAG, "play: connect %s:%u failed errno=%d", host.c_str(), port, errno);
      break;
    }
    ESP_LOGI(TAG, "play: connected %s:%u", host.c_str(), port);
    char req[512];
    snprintf(req, sizeof(req), "GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n", path.c_str(),
             hostport.c_str());
    if (send(sock, req, strlen(req), 0) < 0) {
      ESP_LOGE(TAG, "play: send failed");
      break;
    }
    // 头部读到 \r\n\r\n 为止（头部 + 可能带上的部分 body 都先落在 hdr_buf）
    static const size_t HDR_MAX = 4096;
    static char hdr_buf[HDR_MAX];  // 任务栈小，放静态
    size_t hlen = 0;
    size_t body_in_hdr = 0;
    while (hlen < HDR_MAX) {
      int r = recv(sock, hdr_buf + hlen, HDR_MAX - hlen, 0);
      if (r <= 0) break;
      hlen += r;
      char *p = (char *) memmem(hdr_buf, hlen, "\r\n\r\n", 4);
      if (p != nullptr) {
        body_in_hdr = hlen - (p + 4 - hdr_buf);
        hlen = p + 4 - hdr_buf;
        break;
      }
    }
    if (hlen == 0 || memcmp(hdr_buf, "HTTP/1.", 7) != 0) {
      ESP_LOGE(TAG, "play: bad response header");
      break;
    }
    int status = atoi(hdr_buf + 9);
    int64_t clen = -1;
    bool chunked = false;
    // 逐行找 Content-Length / Transfer-Encoding
    for (char *line = hdr_buf; line < hdr_buf + hlen;) {
      char *e = (char *) memmem(line, hdr_buf + hlen - line, "\r\n", 2);
      size_t ll = e ? (size_t) (e - line) : hdr_buf + hlen - line;
      if (strncasecmp(line, "content-length:", 15) == 0) clen = atoll(line + 15);
      if (strncasecmp(line, "transfer-encoding:", 18) == 0 && strcasestr(line, "chunked") != nullptr) chunked = true;
      line += ll + 2;
    }
    ESP_LOGI(TAG, "play: status=%d clen=%d chunked=%d", status, (int) clen, (int) chunked);
    if (status != 200) break;
    if (clen <= 0 || clen > (int64_t) 12 * 1024 * 1024) {
      // Connection: close 时可以读到断开为止：给个 12MB 上限
      if (clen < 0) clen = 12 * 1024 * 1024;
      else {
        ESP_LOGE(TAG, "play: bad clen");
        break;
      }
    }
    dst = (int16_t *) heap_caps_malloc(clen + 64, MALLOC_CAP_SPIRAM);
    if (dst == nullptr) {
      ESP_LOGE(TAG, "play: psram alloc failed");
      break;
    }
    cap = clen;
    memcpy(dst, hdr_buf + hlen, body_in_hdr);
    len = body_in_hdr;
    while (len < cap) {
      int r = recv(sock, (char *) dst + len, cap - len > 8192 ? 8192 : (int) (cap - len), 0);
      if (r <= 0) break;
      len += r;
    }
    ESP_LOGI(TAG, "play: read %u bytes", (unsigned) len);
  } while (false);
  if (sock >= 0) close(sock);
  if (len == 0) {
    heap_caps_free(dst);
    s_busy = false;
    vTaskDelete(nullptr);
    return;
  }

  uint8_t *b = (uint8_t *) dst;
  size_t off = dst ? wav_data_offset(b, len) : 0;
  uint32_t sr = 0;
  if (off) {
    for (size_t i = 12; i + 16 <= off; i += 8) {
      uint32_t sz = (uint32_t) b[i + 4] | ((uint32_t) b[i + 5] << 8) | ((uint32_t) b[i + 6] << 16) |
                    ((uint32_t) b[i + 7] << 24);
      if (memcmp(b + i, "fmt ", 4) == 0 && sz >= 16) {
        uint16_t ch = b[i + 10] | (b[i + 11] << 8);
        uint32_t rate = (uint32_t) b[i + 12] | ((uint32_t) b[i + 13] << 8) | ((uint32_t) b[i + 14] << 16) |
                        ((uint32_t) b[i + 15] << 24);
        uint16_t bits = b[i + 22] | (b[i + 23] << 8);
        // 22050Hz（MOSS TTS 原生）也收：线性插值重采样到 16k 播放。
        if (ch == 1 && bits == 16 && (rate == 16000 || rate == 8000 || rate == 22050)) sr = rate;
        break;
      }
    }
  }
  if (sr == 0) {
    ESP_LOGE(TAG, "play: not 16bit mono 8k/16k WAV (len=%u, off=%u)", (unsigned) len, (unsigned) off);
  } else {
    size_t n = (len - off) / 2;
    const int16_t *src = (const int16_t *) (b + off);
    int16_t *out = nullptr;
    if (sr == 22050) {
      // 22050→16000 线性插值重采样（MOSS TTS 原生率）
      size_t m = (size_t) ((uint64_t) n * 16000 / 22050);
      int16_t *out2 = (int16_t *) heap_caps_malloc(m * 2 + 64, MALLOC_CAP_SPIRAM);
      if (out2 != nullptr) {
        for (size_t i = 0; i < m; i++) {
          float t = (float) i * 22050 / 16000;
          size_t j = (size_t) t;
          float fr = t - j;
          int32_t a = j < n ? src[j] : src[n - 1];
          int32_t bb = j + 1 < n ? src[j + 1] : src[n - 1];
          out2[i] = (int16_t) (a + (bb - a) * fr);
        }
        n = m;
        out = out2;
      }
    } else if (sr == 16000) {
      out = dst;
      src = (const int16_t *) (b + off);
    } else {
      // 8k→16k：每个样本复制两份
      // 扩 2 倍后的长度不能超过原块（dst 容量 = len），否则尾部越界
      out = (int16_t *) heap_caps_malloc(n * 4 + 64, MALLOC_CAP_SPIRAM);
      if (out != nullptr) {
        for (size_t i = 0; i < n; i++) out[2 * i] = out[2 * i + 1] = src[i];
        n *= 2;
      }
    }
    if (out != nullptr) {
      ESP_LOGI(TAG, "play %us @%uHz (%u KB)", (unsigned) (n / CLEAN_RATE), (unsigned) (sr == 22050 ? 16000 : sr), (unsigned) (len / 1024));
      spk_play_raw(out, n);
      if (out != dst) heap_caps_free(out);
      ESP_LOGI(TAG, "play done");
    } else {
      ESP_LOGE(TAG, "play: psram alloc failed");
    }
  }
  heap_caps_free(dst);
  s_busy = false;
  vTaskDelete(nullptr);
}

// PC 上：python mqpub.py chenvoice/control/node1/play http://192.168.124.x:8000/xxx.wav
// MQTT 回调里只起任务，下载/播放都在任务里跑。
inline void play_url(const std::string &url);
// 开机自测：先从香橙派 8899 的 TTS 服务拿回复音频地址（mock 也会返回 WAV），
// 拿不到就退回 PC 上的固定测试 WAV。两步都失败就不出声。
inline void play_boot() {
  std::string url;
  esp_http_client_config_t cfg{};
  static const char *ASK = "http://192.168.207.150:8899/node/audio";
  cfg.url = ASK;
  cfg.method = HTTP_METHOD_POST;
  cfg.timeout_ms = 8000;
  esp_http_client_handle_t h = esp_http_client_init(&cfg);
  esp_http_client_set_header(h, "Content-Type", "application/json");
  static const char body[] = "{\"device\":\"node1\",\"sr\":16000,\"format\":\"s16le\",\"audio\":\"\"}";
  esp_http_client_set_post_field(h, body, sizeof(body) - 1);
  if (esp_http_client_perform(h) == ESP_OK && esp_http_client_get_status_code(h) == 200) {
    std::string resp;
    char chunk[256];
    int r;
    while ((r = esp_http_client_read(h, chunk, sizeof(chunk))) > 0) {
      resp.append(chunk, r);
      if (resp.size() > 8192) break;
    }
    size_t at = resp.find("\"reply_url\":\"");
    if (at != std::string::npos) {
      size_t s = at + 12, e = resp.find('"', s);
      if (e != std::string::npos && e > s) url = resp.substr(s, e - s);
    }
    ESP_LOGI(TAG, "boot ask: http=%d resp=%.120s", (int) esp_http_client_get_status_code(h), resp.c_str());
  } else {
    ESP_LOGW(TAG, "boot ask failed: http=%d", (int) esp_http_client_get_status_code(h));
  }
  esp_http_client_cleanup(h);
  if (url.empty()) {
    ESP_LOGW(TAG, "boot play: no reply_url, skip");
  } else {
    ESP_LOGI(TAG, "boot play: %s", url.c_str());
    play_url(url);
  }
}

inline void play_url(const std::string &url) {
  if (s_busy) {
    ESP_LOGW(TAG, "play: busy (listen/diag running)");
    return;
  }
  s_busy = true;
  xTaskCreateWithCaps(play_task, "cn_play", 12288, new std::string(url), 2, nullptr,
                      MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
}

// ---- MQTT 分包收音频（抄小智"流式推包"思路，绕开 HTTP 客户端）----
// 香橙派把 16bit mono 16k 裸 PCM 切 ~4KB 块 base64 推：
//   chenvoice/audio/node1/begin  {"sr":16000,"total":N}   开缓冲
//   chenvoice/audio/node1/chunk  <base64>                 追加（QoS0，broker 本机不丢）
//   chenvoice/audio/node1/end    ""                       收完开播
// PC 上一步到位：python mqtt_push_tts.py "要合成的话"
static int16_t *g_mq_audio = nullptr;
static size_t g_mq_audio_cap = 0, g_mq_audio_len = 0;  // 样本数
static bool g_mq_rx = false;
static portMUX_TYPE g_mq_mux = portMUX_INITIALIZER_UNLOCKED;

inline void mq_audio_begin(int total_samples) {
  // Logs must never run inside the critical section: the logger's puts() takes
  // locks/heap and abort()s with interrupts disabled (crashed on a bad total).
  bool bad = total_samples <= 0 || total_samples > CLEAN_RATE * 120;  // 上限 120s
  if (!bad) {
    portENTER_CRITICAL(&g_mq_mux);
    if ((size_t) total_samples * 2 + 1024 > g_mq_audio_cap) {
      heap_caps_free(g_mq_audio);
      g_mq_audio = (int16_t *) heap_caps_malloc(total_samples * 2 + 1024, MALLOC_CAP_SPIRAM);
      g_mq_audio_cap = g_mq_audio ? total_samples : 0;
    }
    g_mq_audio_len = 0;
    g_mq_rx = g_mq_audio != nullptr;
    portEXIT_CRITICAL(&g_mq_mux);
  }
  if (bad) {
    ESP_LOGW(TAG, "mqaudio: bad total %d", total_samples);
  } else if (g_mq_rx) {
    ESP_LOGI(TAG, "mqaudio: begin %ds", total_samples);
  }
}

inline void mq_audio_chunk(const std::string &b64) {
  if (!g_mq_rx) return;
  static uint8_t *dec = nullptr;
  if (dec == nullptr) dec = (uint8_t *) heap_caps_malloc(8192, MALLOC_CAP_SPIRAM);
  if (dec == nullptr) return;
  size_t dlen = 0;
  if (mbedtls_base64_decode(dec, 8192, &dlen, (const uint8_t *) b64.data(), b64.size()) != 0) {
    ESP_LOGW(TAG, "mqaudio: b64 decode failed (%u)", (unsigned) b64.size());
    return;
  }
  portENTER_CRITICAL(&g_mq_mux);
  if (g_mq_audio && g_mq_audio_len + dlen / 2 <= g_mq_audio_cap) {
    memcpy(g_mq_audio + g_mq_audio_len, dec, dlen);
    g_mq_audio_len += dlen / 2;
  }
  portEXIT_CRITICAL(&g_mq_mux);
}

inline void mq_audio_end() {
  portENTER_CRITICAL(&g_mq_mux);
  size_t n = g_mq_audio_len;
  g_mq_rx = false;
  portEXIT_CRITICAL(&g_mq_mux);
  if (n == 0) {
    ESP_LOGW(TAG, "mqaudio: end with 0 samples");
    return;
  }
  ESP_LOGI(TAG, "mqaudio: end %.2fs, playing", n / (float) CLEAN_RATE);
  spk_write(g_mq_audio, n);
  ESP_LOGI(TAG, "mqaudio: play done");
}

// ---- 采集诊断（MQTT chenvoice/control/node1/diag）----
// 0=只统计最近 10s；1=统计+喇叭按 2 倍把最近 10s 放出来；2=1 之后再跑一轮录音上传。
// 在任务里跑，不阻塞主循环。
inline void diag_task(void *arg) {
  int mode = (int) (intptr_t) arg;
  const size_t maxn = CLEAN_RATE * 10;
  int16_t *tmp = (int16_t *) heap_caps_malloc(maxn * 2, MALLOC_CAP_SPIRAM);
  if (tmp == nullptr) {
    ESP_LOGE(TAG, "diag: psram alloc failed");
  } else {
    size_t n = clean_tail(tmp, maxn);
    analyze(tmp, n, "diag");
    if (mode >= 1) spk_play_scaled(tmp, n);
    heap_caps_free(tmp);
  }
  g_listening = false;
  // 模式 3：loopback 并录下麦克风实际收到的声音（看是不是纯脉冲）；
  // 模式 4：loopback 放完立刻开录（没 AEC，开着 mww
  // 喇叭声会再触发唤醒）。
  if (mode == 4) {
    diag_task((void *) (intptr_t) 4);
    vTaskDelete(nullptr);
    return;
  }
  s_busy = false;
  if (mode == 2) listen(6, 800);
  vTaskDelete(nullptr);
}

inline void diag(int mode) {
  if (s_busy) {
    ESP_LOGW(TAG, "diag already running");
    return;
  }
  s_busy = true;
  g_listening = true;  // 和唤醒触发的 listen 互斥
  xTaskCreateWithCaps(diag_task, "cn_diag", 16384, (void *) (intptr_t) mode, 2, nullptr,
                      MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
}

// ---- 导出缓存里的最近 10s（MQTT chenvoice/control/node1/dump）----
// 主循环调用：第一次只拷一份出来（快），第二次再统计+写 320KB 进槽位。
// 然后 PC 上取 http://<node>:9890/fn/listen.wav。
inline void dump_clean() {
  static int16_t *tmp = nullptr;
  static bool primed = false;
  const size_t maxn = CLEAN_RATE * 10;
  if (tmp == nullptr) tmp = (int16_t *) heap_caps_malloc(maxn * 2, MALLOC_CAP_SPIRAM);
  if (tmp == nullptr) return;
  size_t n = clean_tail(tmp, maxn);
  if (!primed) {
    primed = true;
    // mwwprobe counters live in micro_wake_word.cpp — task-context logs never reach the
    // log transport here, so print them from this main-loop context instead.
    ESP_LOGI(TAG,
             "mwwstat cb=%u drop=%u loop=%u starve=%u feat=%u avail=%u use=%u st=%u",
             (unsigned) g_mww_probe_cb, (unsigned) g_mww_probe_cb_drop, (unsigned) g_mww_probe_loop,
             (unsigned) g_mww_probe_starve, (unsigned) g_mww_probe_feat, (unsigned) g_mww_probe_avail,
             (unsigned) g_mww_probe_use, (unsigned) g_mww_probe_st);
    ESP_LOGI(TAG, "dump primed %.2fs (send again to export)", n / 16000.0);
    return;
  }
  analyze(tmp, n, "dump");
  fn_begin("listen.wav");
  fn_write_wav(tmp, n, CLEAN_RATE);
  ESP_LOGI(TAG, "dump %.2fs WAVB:listen.wav", n / 16000.0);
}

// ---- 主循环维护 ----
inline void cn_maintain() {
  if (g_listening && s_listen_deadline && (int32_t) (millis() - s_listen_deadline) > 0) {
    s_listen_deadline = 0;
    ESP_LOGW(TAG, "listen stuck >%us (offline?), resuming wake word", (unsigned) (UPLOAD_GUARD_MS / 1000));
    mww_start_owned();
  }
}

// 音频来源：mic 上唯一的读者是 mww 的 mic_task，它把每帧数据交给注册在 mic 上的
// 回调。我们在 on_data 里拿同一份数据：32bit 槽取高 16 位存进干净缓存。
inline void cn_sample_cb(const std::vector<uint8_t> &data) {
  if (g_clean == nullptr || data.size() < 16) return;
  const int32_t *pcm = (const int32_t *) data.data();
  size_t n = data.size() / 4;
  static uint32_t cb_count = 0;
  portENTER_CRITICAL(&g_clean_mux);
  int16_t peak = 0;
  for (size_t i = 0; i < n; i++) {
    // INMP441 实录电平只有几个百分点 FS（喊话 rms≈1%），tflite 特征按满量程训练，
    // 不放大模型根本看不到。放大只作用于我们自己的缓存/导出，不影响 mww 的输入。
    int32_t v32 = (pcm[i] >> 16) * 16;
    int16_t v = v32 > 32767 ? 32767 : (v32 < -32768 ? -32768 : (int16_t) v32);
    g_clean[g_clean_w] = v;
    g_clean_w = (g_clean_w + 1) % (CLEAN_RATE * CLEAN_SECONDS);
    if (g_clean_len < (size_t) CLEAN_RATE * CLEAN_SECONDS) g_clean_len++;
    int16_t a = v < 0 ? (int16_t) -v : v;
    if (a > peak) peak = a;
  }
  g_cb_samples += n;
  portEXIT_CRITICAL(&g_clean_mux);
  if (s_cb_task[0] == 0 && n > 10) strncpy(s_cb_task, pcTaskGetName(nullptr), sizeof(s_cb_task) - 1);
  if (++cb_count % 100 == 1) ESP_LOGI(TAG, "mic: peak=%d n=%u sz=%u", peak, (unsigned) n, (unsigned) data.size());
}

inline void clean_init() {
  g_clean = (int16_t *) heap_caps_malloc(CLEAN_RATE * CLEAN_SECONDS * sizeof(int16_t), MALLOC_CAP_SPIRAM);
  if (g_clean == nullptr) ESP_LOGE(TAG, "clean buffer alloc failed");
  g_snap = (int16_t *) heap_caps_malloc(CLEAN_RATE * 7 * 2, MALLOC_CAP_SPIRAM);
  g_snap_cap = g_snap != nullptr ? CLEAN_RATE * 7 : 0;
  fn_init();
  fn_server_start();
}

}  // namespace chenvoice
