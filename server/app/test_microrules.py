# -*- coding: utf-8 -*-
"""Tests for microrules.py: atomic 开/关+设备+房间 fast path.

Run: PYTHONUTF8=1 python test_microrules.py  (exit code 0 = all pass)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

import microrules

FAILS = []
N = [0]


def check(name, cond, detail=""):
    N[0] += 1
    status = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print("[%s] %s %s" % (status, name, detail if not cond else ""))


# --- hits: full-element atomic commands -------------------------------------

HITS = [
    ("打开客厅的灯", "light_on", "living_room"),
    ("关掉卧室的空调", "ac_off", "bedroom"),
    ("开灯", "light_on", "none"),
    ("把电视关了", "tv_off", "none"),
    ("打开窗帘", "curtain_open", "none"),
    ("开一下厨房的风扇", "fan_on", "kitchen"),
    ("把阳台的灯打开呗", "light_on", "balcony"),
    ("帮我打开所有的灯", "light_on", "all"),
    ("关闭儿童房的灯", "light_off", "kids_room"),
    ("开空调", "ac_on", "none"),
    ("关上书房的灯", "light_off", "study"),
    ("把餐厅的风扇关掉吧", "fan_off", "dining_room"),
    ("拉开卧室的窗帘", "curtain_open", "bedroom"),
    ("启动电视", "tv_on", "none"),
]

for text, action, room in HITS:
    r = microrules.match(text)
    ok = r is not None and r["action"] == action and r["room"] == room
    check("hit:%s" % text, ok, r)

# --- defers: model's job ----------------------------------------------------

DEFERS = [
    "别开灯",              # negation
    "不要关空调",          # negation
    "先不开灯",            # negation
    "灯开着吗",            # question
    "客厅灯开着吗",        # question
    "开灯然后关空调",      # compound
    "打开客厅的灯和卧室的灯",  # compound
    "帮我把燃气灶打开",    # unknown device
    "你都会干嘛",          # chitchat
    "开着灯睡觉",          # leftover content
    "太阳能板打开",        # absent device
    "把灯调亮",            # not an on/off op
    "灯为什么打不开",      # question+unknown
    "",                    # empty
    "打开",                # verb only, no device
    "灯",                  # device only, no verb
]

for text in DEFERS:
    r = microrules.match(text)
    check("defer:%s" % (text or "<empty>"), r is None, r)

# --- result shape -----------------------------------------------------------

r = microrules.match("打开客厅的灯")
check("shape_keys",
      r and set(r) >= {"action", "room", "text", "source"},
      r)
check("shape_source", r and r["source"] == "microrules", r)
check("shape_text", r and r["text"] == "打开客厅的灯", r)

print("Result: %d/%d" % (N[0] - len(FAILS), N[0]))
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
