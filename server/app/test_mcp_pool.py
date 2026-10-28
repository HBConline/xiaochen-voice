# -*- coding: utf-8 -*-
"""Tests for mcp_pool.py: stdio ToolPool over MCP-subset servers.

Run: PYTHONUTF8=1 python test_mcp_pool.py  (exit code 0 = all pass)
"""
import os
import sys
import tempfile
import textwrap
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "servers"))
sys.stdout.reconfigure(encoding="utf-8")

os.environ["DEVICE_BACKEND"] = "mock"
os.environ["DEVICE_STATE_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="mcp_pool_test_"), "state.json"
)

import mcp_pool

FAILS = []
N = [0]


def check(name, cond, detail=""):
    N[0] += 1
    status = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print("[%s] %s %s" % (status, name, detail if not cond else ""))


HERE = os.path.dirname(os.path.abspath(__file__))
HOME_MCP = os.path.join(HERE, "servers", "home_mcp.py")

# A server that hangs on tools/call, for the timeout path.
slow_path = os.path.join(tempfile.mkdtemp(prefix="mcp_slow_"), "slow_mcp.py")
with open(slow_path, "w", encoding="utf-8") as f:
    f.write(textwrap.dedent('''
        import json, sys, time
        for line in sys.stdin:
            req = json.loads(line)
            if req.get("method") == "initialize":
                print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {
                    "protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "slow", "version": "0"}}}))
            elif req.get("method") == "tools/list":
                print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {
                    "tools": [{"name": "sleepy", "description": "hangs",
                               "inputSchema": {"type": "object"}}]}}))
            elif req.get("method") == "tools/call":
                time.sleep(30)
            sys.stdout.flush()
    '''))

pool = mcp_pool.ToolPool(servers=[HOME_MCP], timeout=5)
pool.start()
check("start", pool.alive())

# --- schemas aggregation ----------------------------------------------------

schemas = pool.schemas()
names = sorted(t["function"]["name"] for t in schemas)
check("schemas_names", names == ["list_rooms", "query_device", "run_scene", "set_device"], names)
check("schemas_openai_shape",
      all(t["type"] == "function" and t["function"]["parameters"]["type"] == "object"
          for t in schemas))

# --- tool call --------------------------------------------------------------

r = pool.call("set_device", {"device": "light", "on": True, "room": "living_room"})
check("call_set_device", "客厅" in r and "灯" in r, r)
r = pool.call("query_device", {"device": "light", "room": "living_room"})
check("call_query", "开" in r, r)
r = pool.call("nope", {})
check("call_unknown_tool", "没有" in r or "error" in r.lower(), r)

# --- crash restart ----------------------------------------------------------

proc = next(iter(pool._by_script.values())).proc
proc.kill()
proc.wait(timeout=5)
r = pool.call("list_rooms", {})
check("restart_after_kill", "房间" in r, r)
check("restart_alive", pool.alive())

# --- timeout ----------------------------------------------------------------

slow = mcp_pool.ToolPool(servers=[slow_path], timeout=1)
slow.start()
t0 = time.time()
r = slow.call("sleepy", {})
dt = time.time() - t0
check("timeout_fires", dt < 5 and ("超时" in r or "timeout" in r.lower()), (dt, r))
slow.close()

# --- close ------------------------------------------------------------------

pool.close()
check("closed", not pool.alive())

print("Result: %d/%d" % (N[0] - len(FAILS), N[0]))
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
