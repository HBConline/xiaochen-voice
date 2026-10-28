# -*- coding: utf-8 -*-
"""B3 wiring tests: intent.understand microrules-first + voice_brain tools loop.

Run: PYTHONUTF8=1 python test_intent_b3.py  (exit code 0 = all pass)
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

os.environ["DEVICE_BACKEND"] = "mock"
os.environ["DEVICE_STATE_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="intent_b3_test_"), "state.json"
)
os.environ["LOG_MISSES"] = "0"

# Stub llama-server: scripted tool_call -> final text, records request bodies.
SCRIPT = []
REQUESTS = []


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        REQUESTS.append(body)
        reply = SCRIPT.pop(0) if SCRIPT else {"content": "好"}
        msg = {"role": "assistant", "content": reply.get("content")}
        if reply.get("tool_calls"):
            msg["tool_calls"] = reply["tool_calls"]
            msg["content"] = None
        data = json.dumps({"choices": [{"message": msg}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


_srv = HTTPServer(("127.0.0.1", 0), _Handler)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
os.environ["BRAIN_URL"] = "http://127.0.0.1:%d/v1/chat/completions" % _srv.server_port

import intent
import tts
import voice_brain
import home_config

FAILS = []
N = [0]


def check(name, cond, detail=""):
    N[0] += 1
    status = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print("[%s] %s %s" % (status, name, detail if not cond else ""))


# --- intent.understand: microrules fast path --------------------------------

r = intent.understand("打开客厅的灯")
c = r["commands"][0]
check("u_hit_action", c["action"] == "light_on" and c["room"] == "living_room", c)
check("u_hit_source", c.get("source") == "microrules", c)
check("u_hit_shape", c.get("urgency") == 0.0 and "supported" in c, c)

r = intent.understand("开灯然后关空调")
acts = [(c["action"], c["room"]) for c in r["commands"]]
check("u_compound_two", acts == [("light_on", "none"), ("ac_off", "none")], acts)

r = intent.understand("开客厅的灯和卧室的空调")
acts = [(c["action"], c["room"]) for c in r["commands"]]
check("u_conj_two", acts == [("light_on", "living_room"), ("ac_on", "bedroom")], acts)

r = intent.understand("客厅的灯和卧室的灯都关掉")
acts = [(c["action"], c["room"]) for c in r["commands"]]
check("u_multiroom", acts == [("light_off", "living_room"), ("light_off", "bedroom")], acts)

# --- intent.understand: defers ----------------------------------------------

for text in ("灯开着吗", "别开灯", "晚安", "今天天气怎么样", "你好呀"):
    r = intent.understand(text)
    c = r["commands"][0]
    check("u_defer:%s" % text,
          c["action"] == "other" and c.get("source") == "defer", c)

# absent device stays an honest refusal, never a model improvisation
r = intent.understand("打开太阳能板")
c = r["commands"][0]
check("u_absent", c["action"] == "absent_device" and c.get("absent_device") == "太阳能板", c)
r = intent.understand("帮我把燃气灶打开")
c = r["commands"][0]
check("u_absent2", c["action"] == "absent_device", c)

# --- cache still works ------------------------------------------------------

r1 = intent.understand("关掉卧室的风扇")
r2 = intent.understand("关掉卧室的风扇")
check("u_cache", r1 is r2 and r2["commands"][0]["action"] == "fan_off")

# --- voice_brain tools loop -------------------------------------------------

SCRIPT.append({
    "tool_calls": [{
        "id": "call_1", "type": "function",
        "function": {"name": "set_device",
                     "arguments": json.dumps({"device": "light", "on": True,
                                              "room": "living_room"})},
    }],
})
SCRIPT.append({"content": "客厅灯已经开了。"})
REQUESTS.clear()
text = voice_brain._chat_tools("打开客厅的灯")
check("t_final_text", text == "客厅灯已经开了。", text)
check("t_two_rounds", len(REQUESTS) == 2, len(REQUESTS))
check("t_tools_sent", any(
    t.get("function", {}).get("name") == "set_device"
    for t in REQUESTS[0].get("tools", [])), REQUESTS[0].get("tools"))
check("t_tool_result_fed", any(
    m.get("role") == "tool" and "客厅" in m.get("content", "")
    for m in REQUESTS[1].get("messages", [])), REQUESTS[1]["messages"])

# no tool call -> single round, plain text
SCRIPT.append({"content": "你好呀。"})
REQUESTS.clear()
text = voice_brain._chat_tools("你好")
check("t_plain", text == "你好呀。" and len(REQUESTS) == 1, (text, len(REQUESTS)))

# runaway tool calls -> give up cleanly (empty string, no crash)
SCRIPT.clear()
for _ in range(5):
    SCRIPT.append({"tool_calls": [{
        "id": "c", "type": "function",
        "function": {"name": "list_rooms", "arguments": "{}"},
    }]})
REQUESTS.clear()
text = voice_brain._chat_tools("一直调工具")
check("t_rounds_cap", text == "" and len(REQUESTS) == 3, (text, len(REQUESTS)))

# unknown tool call name -> error text fed back, loop continues
SCRIPT.clear()
SCRIPT.append({"tool_calls": [{
    "id": "c", "type": "function",
    "function": {"name": "nope", "arguments": "{}"},
}]})
SCRIPT.append({"content": "抱歉。"})
text = voice_brain._chat_tools("坏工具")
check("t_bad_tool", text == "抱歉。", text)

# reply() routes action="other" through the tools loop
SCRIPT.clear()
SCRIPT.append({"content": "今天周二。"})
REQUESTS.clear()
out = voice_brain.reply("解释一下相对论", {"action": "other", "room": "none",
                                      "text": "解释一下相对论",
                                      "commands": [{"action": "other", "room": "none",
                                                    "text": "解释一下相对论"}]})
check("reply_other_uses_tools",
      out == "今天周二。" and any("tools" in body for body in REQUESTS), (out, REQUESTS))

# --- step 5: answer heap deleted, vocabulary centralised ----------------------

check("find_room", home_config.find_room("客厅的灯") == "living_room")
check("find_room_longest", home_config.find_room("宝宝房的灯") == "kids_room")
check("find_room_none", home_config.find_room("打开灯") in (None, ""))

# device status question goes to the model + query_device tool (B3: 查询 →
# home_mcp 工具), not a hardcoded answer in voice_brain
SCRIPT.clear()
SCRIPT.append({"content": "客厅灯开着呢。"})
REQUESTS.clear()
qi = intent.understand("客厅灯开着吗")
out = voice_brain.reply("客厅灯开着吗", qi)
check("query_uses_tools",
      out == "客厅灯开着呢。" and len(REQUESTS) == 1
      and "tools" in REQUESTS[0] and "当前状态" in REQUESTS[0]["messages"][1]["content"],
      (out, len(REQUESTS), REQUESTS[0]["messages"][1]["content"] if REQUESTS else None))

# tts.make_reply = 执行结果拼装 only (per-action template table deleted)
voice_brain._clear_results()
voice_brain._LAST_DEVICE_RESULT[("light_on", "living_room")] = {
    "ok": True, "supported": True, "executed": ["living_room"],
    "failed": [], "detail": ""}
r = tts.make_reply({"commands": [{"action": "light_on", "room": "living_room"}]})
check("mr_result", r == "客厅灯开好了。", r)
voice_brain._LAST_DEVICE_RESULT[("absent_device", "none")] = {
    "ok": True, "supported": False, "executed": [], "failed": [],
    "detail": "家里没有太阳能板，换不了它。"}
r = tts.make_reply({"commands": [{"action": "absent_device", "room": "none"}]})
check("mr_refuse", r == "家里没有太阳能板，换不了它。", r)
r = tts.make_reply({"commands": [{"action": "other", "room": "none"}]})
check("mr_empty", r is None, r)

print("Result: %d/%d" % (N[0] - len(FAILS), N[0]))
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
