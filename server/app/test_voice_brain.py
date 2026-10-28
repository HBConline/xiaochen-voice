# -*- coding: utf-8 -*-
"""Smoke test: voice brain via llama-server matches chat_test expectations."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

import voice_brain

CASES = [
    # (question, intent dict, expected flavor)
    ("把客厅的灯打开", {"action": "light_on", "room": "living_room", "text": "把客厅的灯打开"}, "confirm"),
    ("呃那个卧室的空调帮我开到26度呗", {"action": "temp_set", "room": "bedroom", "text": "呃那个卧室的空调帮我开到26度呗"}, "confirm"),
    ("我回来了", {"action": "scene_home", "room": "none", "text": "我回来了"}, "scene"),
    ("查一下客厅的灯", {"action": "light_on", "room": "living_room", "text": "查一下客厅的灯"}, "no-context-status"),
    ("帮我把燃气灶打开", {"action": "other", "room": "none", "text": "帮我把燃气灶打开"}, "safety"),
    ("客厅的灯怎么打不开", {"action": "other", "room": "living_room", "text": "客厅的灯怎么打不开"}, "fail-help"),
    ("你都会干嘛", {"action": "other", "room": "none", "text": "你都会干嘛"}, "chitchat"),
]

ok = 0
for q, intent, flavor in CASES:
    ctx = voice_brain.build_context(intent)
    content = voice_brain.build_prompt(q, ctx)
    text = voice_brain.reply(q, intent)
    leak = "TOOL!" if text and "tool_call" in text else "ok"
    status = "PASS" if text and leak == "ok" else "FAIL"
    if status == "PASS":
        ok += 1
    print("[%s][%s] Q: %s" % (status, leak, q))
    print("  ctx: %s" % (ctx or "-"))
    print("  A: %s" % text)
    print("---")

print("Result: %d/%d" % (ok, len(CASES)))
