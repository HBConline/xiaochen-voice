# -*- coding: utf-8 -*-
"""Tests for servers/home_mcp.py: MCP tool layer over device_layer.

Run: PYTHONUTF8=1 python test_home_mcp.py  (exit code 0 = all pass)
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "servers"))
sys.stdout.reconfigure(encoding="utf-8")

# Isolate hardware/state before device_layer is imported anywhere.
os.environ["DEVICE_BACKEND"] = "mock"
os.environ["DEVICE_STATE_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="home_mcp_test_"), "state.json"
)

import device_layer
import home_mcp

FAILS = []
N = [0]


def check(name, cond, detail=""):
    N[0] += 1
    status = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print("[%s] %s %s" % (status, name, detail if not cond else ""))


def fresh():
    """Fresh mock controller injected into home_mcp (no singleton, no disk)."""
    ctl = device_layer.DeviceController(device_layer.MockBackend())
    home_mcp.set_controller(ctl)
    return ctl


# --- tool: set_device / query_device ----------------------------------------

fresh()
r = home_mcp.tool_set_device("light", True, "living_room")
check("set_light_on_ok", r["ok"], r)
check("set_light_on_query", home_mcp.tool_query_device("light", "living_room")["on"] is True)

r = home_mcp.tool_set_device("light", False, "living_room")
check("set_light_off", home_mcp.tool_query_device("light", "living_room")["on"] is False)

r = home_mcp.tool_set_device("light", True, "all")
check("set_light_all", r["ok"], r)
for room in ("living_room", "bedroom", "kitchen"):
    check("set_light_all_%s" % room,
          home_mcp.tool_query_device("light", room)["on"] is True)

# curtain maps to curtain_open/curtain_close (state-only, still ok)
r = home_mcp.tool_set_device("curtain", True, "bedroom")
check("set_curtain_open", r["ok"], r)

# --- validation -------------------------------------------------------------

r = home_mcp.tool_set_device("foo", True, "living_room")
check("bad_device", not r["ok"] and "设备" in r["error"], r)
r = home_mcp.tool_set_device("light", True, "garage")
check("bad_room", not r["ok"] and "房间" in r["error"], r)
r = home_mcp.tool_set_device("light", "yes", "living_room")
check("bad_on", not r["ok"], r)

# unknown device query -> None state, not an exception
r = home_mcp.tool_query_device("ac", "study")
check("query_unknown_state", r["ok"] and r["on"] is None, r)

# --- tool: run_scene --------------------------------------------------------

fresh()
r = home_mcp.tool_run_scene("home")
check("scene_home", r["ok"], r)
check("scene_home_light",
      home_mcp.tool_query_device("light", "living_room")["on"] is True)
r = home_mcp.tool_run_scene("sleep", "bedroom")
check("scene_sleep", r["ok"], r)
r = home_mcp.tool_run_scene("no_such_scene")
check("scene_bad", not r["ok"], r)

# --- tool: list_rooms -------------------------------------------------------

r = home_mcp.tool_list_rooms()
check("list_rooms", r["ok"] and "客厅" in r["text"], r)

# --- MCP JSON-RPC protocol --------------------------------------------------

resp = home_mcp.handle_request({
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "test", "version": "0"}},
})
check("rpc_initialize", resp and resp.get("result", {}).get("serverInfo", {}).get("name")
      == "home-mcp", resp)

resp = home_mcp.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
tools = (resp or {}).get("result", {}).get("tools", [])
check("rpc_tools_list", len(tools) == 4, tools)
check("rpc_tools_schema",
      all("inputSchema" in t and "name" in t for t in tools))

resp = home_mcp.handle_request({
    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
    "params": {"name": "set_device",
               "arguments": {"device": "light", "on": True, "room": "kitchen"}},
})
content = (resp or {}).get("result", {}).get("content", [])
check("rpc_tools_call",
      content and content[0]["type"] == "text" and "厨房" in content[0]["text"],
      resp)
check("rpc_call_not_error", not (resp or {}).get("result", {}).get("isError"))

resp = home_mcp.handle_request({
    "jsonrpc": "2.0", "id": 4, "method": "tools/call",
    "params": {"name": "set_device", "arguments": {"device": "foo"}},
})
check("rpc_call_bad_tool_args", (resp or {}).get("result", {}).get("isError") is True, resp)

resp = home_mcp.handle_request({"jsonrpc": "2.0", "id": 5, "method": "nope"})
check("rpc_unknown_method", (resp or {}).get("error", {}).get("code") == -32601, resp)

resp = home_mcp.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"})
check("rpc_notification_no_reply", resp is None, resp)

# --- stdio loop round-trip (one request, one response line) ------------------

fresh()
req = {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
       "params": {"name": "query_device",
                  "arguments": {"device": "light", "room": "kitchen"}}}
out = home_mcp.serve_lines([json.dumps(req, ensure_ascii=False)])
check("stdio_roundtrip", out and json.loads(out[0])["id"] == 9, out)

print("Result: %d/%d" % (N[0] - len(FAILS), N[0]))
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
