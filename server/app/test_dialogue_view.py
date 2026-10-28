# -*- coding: utf-8 -*-
"""Tests for the ST7789 dialogue view (pure logic + PIL frame render).

Layout contract:
  top bar   = clock + status dot/word
  middle    = latest one Q&A pair (一问一答)
  remainder = device status rows (from /state groups)

Run: PYTHONUTF8=1 python test_dialogue_view.py  (exit code 0 = all pass)
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

import dialogue_view

FAILS = []
N = [0]


def check(name, cond, detail=""):
    N[0] += 1
    status = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print("[%s] %s %s" % (status, name, detail if not cond else ""))


# --- text units / wrapping ---------------------------------------------------

check("units_cjk_ascii",
      dialogue_view.text_units("你好ab") == 2 + 2 * 0.5,
      dialogue_view.text_units("你好ab"))

check("wrap_cjk", dialogue_view.wrap_text("一二三四五六七八", 3) == ["一二三", "四五六", "七八"],
      dialogue_view.wrap_text("一二三四五六七八", 3))

check("wrap_ascii_words", dialogue_view.wrap_text("hello world", 5) == ["hello", "world"],
      dialogue_view.wrap_text("hello world", 5))

check("wrap_mixed_hard_break", dialogue_view.wrap_text("灯a灯", 2.0) == ["灯a", "灯"],
      dialogue_view.wrap_text("灯a灯", 2.0))

check("wrap_empty", dialogue_view.wrap_text("", 10) == [], None)

# --- transcript from mic_status events --------------------------------------

EV_NODE = {"kind": "turn", "ts": 1.0, "said": "打开客厅的灯",
           "reply": "客厅灯开啦", "source": "rule", "device": "node1"}
EV_MIC = {"kind": "turn", "ts": 2.0, "said": "关闭客厅的灯",
          "reply": {"text": "客厅灯关啦", "source": "rule",
                    "audio": "x.wav", "ts": 2.1}}
EV_WAKE = {"kind": "wake", "ts": 0.5, "keyword": "xiaochen", "via": "node"}
EV_ERR = {"kind": "error", "ts": 3.0, "said": "坏句子", "error": "boom"}

tr = dialogue_view.build_transcript([EV_NODE])
check("transcript_node_reply_str",
      tr == [("you", "打开客厅的灯"), ("bot", "客厅灯开啦")], tr)

tr = dialogue_view.build_transcript([EV_MIC])
check("transcript_mic_reply_dict",
      tr == [("you", "关闭客厅的灯"), ("bot", "客厅灯关啦")], tr)

tr = dialogue_view.build_transcript([EV_WAKE])
check("transcript_skips_wake", tr == [], tr)

tr = dialogue_view.build_transcript([EV_ERR])
check("transcript_error_marked",
      tr == [("you", "坏句子"), ("err", "（出错）")], tr)

tr = dialogue_view.build_transcript([EV_MIC, EV_NODE], max_turns=6)
check("transcript_chronological",
      tr[0][1] == "打开客厅的灯" and tr[2][1] == "关闭客厅的灯", tr)

tr = dialogue_view.build_transcript([EV_ERR, EV_MIC, EV_NODE], max_turns=1)
check("transcript_cap_keeps_newest",
      tr == [("you", "坏句子"), ("err", "（出错）")], tr)

# --- clock -------------------------------------------------------------------

check("clock_format",
      dialogue_view.format_clock(0) == time.strftime('%H:%M:%S', time.localtime(0)),
      dialogue_view.format_clock(0))

# --- device status rows (/state groups) -------------------------------------

G_HOUSE = {"device": "light", "device_zh": "客厅灯", "scope": "house",
           "on": True, "ago": 3.0, "fresh": True, "exceptions": []}
G_MIXED = {"device": "ac", "device_zh": "空调", "scope": "mixed",
           "on": False, "ago": 9.0, "fresh": False,
           "exceptions": [{"room_zh": "卧室", "on": True, "room": "bedroom"}]}
G_MIXED2 = {"device": "ac", "device_zh": "空调", "scope": "mixed",
            "on": False, "ago": 9.0, "fresh": False,
            "exceptions": [{"room_zh": "卧室", "on": True, "room": "bedroom"},
                           {"room_zh": "书房", "on": False, "room": "study"}]}

check("device_row_house_on",
      dialogue_view.device_row_text(G_HOUSE) == "客厅灯 开",
      dialogue_view.device_row_text(G_HOUSE))
check("device_row_house_off",
      dialogue_view.device_row_text(dict(G_HOUSE, on=False)) == "客厅灯 关",
      dialogue_view.device_row_text(dict(G_HOUSE, on=False)))
check("device_row_mixed",
      dialogue_view.device_row_text(G_MIXED) == "空调 关（卧室:开）",
      dialogue_view.device_row_text(G_MIXED))
check("device_row_mixed_multi",
      dialogue_view.device_row_text(G_MIXED2) == "空调 关（卧室:开、书房:关）",
      dialogue_view.device_row_text(G_MIXED2))
check("device_row_no_zh",
      dialogue_view.device_row_text({"device": "fan", "on": True, "scope": "house"})
      == "fan 开")

# --- state meta --------------------------------------------------------------

for st in ("off", "waiting_wake", "listening", "processing"):
    meta = dialogue_view.state_meta(st)
    check("state_meta_" + st,
          isinstance(meta["label"], str) and len(meta["color"]) == 3, meta)

check("blink_states",
      dialogue_view.state_meta("listening")["blink"]
      and dialogue_view.state_meta("processing")["blink"]
      and not dialogue_view.state_meta("waiting_wake")["blink"])

check("state_meta_unknown", dialogue_view.state_meta("???")["label"] == "待机")

check("labels_zh",
      dialogue_view.state_meta("listening")["label"] == "聆听中"
      and dialogue_view.state_meta("processing")["label"] == "思考中"
      and dialogue_view.state_meta("speaking")["label"] == "说话中",
      [dialogue_view.state_meta(s)["label"] for s in ("listening", "processing", "speaking")])

check("speaking_meta",
      dialogue_view.state_meta("speaking")["blink"]
      and len(dialogue_view.state_meta("speaking")["color"]) == 3
      and dialogue_view.state_meta("speaking")["color"]
      != dialogue_view.state_meta("listening")["color"],
      dialogue_view.state_meta("speaking"))

# --- rendered frame ---------------------------------------------------------

font = dialogue_view.resolve_font()
check("font_found", font is not None and os.path.isfile(font), font)

img = dialogue_view.render_frame("listening", [EV_NODE], now=0.25, font_path=font)
check("frame_size", img.size == (240, 240) and img.mode == "RGB", (img.size, img.mode))

meta = dialogue_view.state_meta("listening")
px = img.getpixel((dialogue_view.DOT_CX, dialogue_view.DOT_CY))
check("dot_color_on_phase", px == meta["color"], (px, meta["color"]))

img2 = dialogue_view.render_frame("listening", [EV_NODE], now=0.75, font_path=font)
px2 = img2.getpixel((dialogue_view.DOT_CX, dialogue_view.DOT_CY))
check("dot_blink_dark_phase", px2 != meta["color"], (px2, meta["color"]))

img3 = dialogue_view.render_frame("waiting_wake", [EV_NODE], now=1.0, font_path=font)
px3 = img3.getpixel((dialogue_view.DOT_CX, dialogue_view.DOT_CY))
check("dot_static_when_idle",
      px3 == dialogue_view.state_meta("waiting_wake")["color"], px3)

# dialogue zone (between top bar and divider) has text
dialog = img.crop((0, dialogue_view.TOP_H, 240, dialogue_view.DIVIDER_Y))
colors = dialog.getcolors(maxcolors=100000)
non_bg = sum(c for c, col in colors if col != dialogue_view.BG)
check("dialogue_zone_has_text", non_bg > 100, non_bg)

# device zone empty when no devices given
img4 = dialogue_view.render_frame("off", [], now=0.0, font_path=font)
check("frame_empty_events_ok", img4.size == (240, 240))

# device zone shows rows when devices present
img5 = dialogue_view.render_frame("waiting_wake", [EV_NODE], devices=[G_HOUSE, G_MIXED],
                                  now=1.0, font_path=font)
dev_zone = img5.crop((0, dialogue_view.DIVIDER_Y, 240, 240))
colors = dev_zone.getcolors(maxcolors=100000)
non_bg = sum(c for c, col in colors if col != dialogue_view.BG)
check("device_zone_has_text", non_bg > 150, non_bg)

# on/off dots in device zone use the green/gray pair
dot_on = img5.getpixel((dialogue_view.DEV_DOT_X, dialogue_view.DEV_ROW0_Y + 4))
check("device_dot_on_color", dot_on == dialogue_view.DEV_ON, (dot_on,))

img6 = dialogue_view.render_frame("waiting_wake", [], devices=[dict(G_HOUSE, on=False)],
                                  now=1.0, font_path=font)
dot_off = img6.getpixel((dialogue_view.DEV_DOT_X, dialogue_view.DEV_ROW0_Y + 4))
check("device_dot_off_color", dot_off == dialogue_view.DEV_OFF, (dot_off,))

# latest pair only: two turns in, one pair-worth of "你:" rows out of history
img7 = dialogue_view.render_frame("waiting_wake", [EV_MIC, EV_NODE], now=1.0,
                                  font_path=font)
check("one_pair_zone_height",
      dialogue_view.DIVIDER_Y - dialogue_view.TOP_H <= 96)

print("%d checks, %d fail" % (N[0], len(FAILS)))
if FAILS:
    print("FAILED:", ", ".join(FAILS))
sys.exit(1 if FAILS else 0)
