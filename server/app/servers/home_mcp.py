# -*- coding: utf-8 -*-
"""MCP tools server: device control over device_layer (stdio JSON-RPC).

One tool surface the language model can call: set_device / query_device /
run_scene / list_rooms. Wraps device_layer so the hardware path is exactly the
one intent already uses — this module adds no device logic of its own, only
validation and spoken-Chinese result text.

Protocol: minimal MCP subset over newline-delimited JSON-RPC 2.0 on stdio —
initialize, tools/list, tools/call, notifications/initialized. No SDK
dependency; mcp_pool.py is the client and speaks the same subset.
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import device_layer
import scenes
from home_config import (
    ACTION_DEVICE,
    DEVICE_CAPS,
    DEVICE_ZH,
    ROOM_ZH,
    ROOMS,
)

# device+on -> action name, derived from the canonical table (no drift).
_SET_ACTION = {(dev, on): act for act, (dev, on) in ACTION_DEVICE.items()}

_VALID_DEVICES = tuple(sorted({dev for dev, _on in _SET_ACTION}))
_VALID_ROOMS = tuple(ROOMS) + ("all", "none")

_SCENES = {s[len("scene_"):]: s for s in scenes.SCENE_ACTIONS}

_ctl = None


def set_controller(ctl) -> None:
    """Inject the device controller (tests use a mock-backed one)."""
    global _ctl
    _ctl = ctl


def _controller():
    global _ctl
    if _ctl is None:
        _ctl = device_layer.get_controller()
    return _ctl


# ---------------------------------------------------------------------------
# Tools (plain functions; unit-tested directly, no protocol involved)
# ---------------------------------------------------------------------------

def tool_set_device(device: str, on, room: str) -> dict:
    """Switch one device class in one room on/off."""
    if device not in _VALID_DEVICES:
        return {"ok": False,
                "error": "不认识的设备：%s。可选：%s" % (device, "、".join(_VALID_DEVICES))}
    if not isinstance(on, bool):
        return {"ok": False, "error": "on 必须是 true 或 false"}
    if room not in _VALID_ROOMS:
        return {"ok": False,
                "error": "不认识的房间：%s。可选：%s" % (room, "、".join(ROOM_ZH[r] for r in _VALID_ROOMS))}
    action = _SET_ACTION[(device, on)]
    result = _controller().execute(action, room)
    if not result.get("ok"):
        failed = "、".join(ROOM_ZH.get(r, r) for r in result.get("failed", []))
        return {"ok": False,
                "error": "%s没成功：%s" % (DEVICE_ZH.get(device, device), failed)}
    rooms = "、".join(ROOM_ZH.get(r, r) for r in result.get("executed") or [room])
    state = "开" if on else "关"
    if device == "curtain":
        state = "拉开" if on else "拉上"
    return {"ok": True, "action": action, "room": room,
            "text": "%s%s已%s" % (rooms, DEVICE_ZH.get(device, device), state)}


def tool_query_device(device: str, room: str) -> dict:
    """Ask whether a device is currently on."""
    if device not in _VALID_DEVICES:
        return {"ok": False,
                "error": "不认识的设备：%s。可选：%s" % (device, "、".join(_VALID_DEVICES))}
    if room not in _VALID_ROOMS:
        return {"ok": False,
                "error": "不认识的房间：%s" % room}
    state = _controller().query(device, room)
    room_zh = ROOM_ZH.get(room, room)
    dev_zh = DEVICE_ZH.get(device, device)
    if state is None:
        return {"ok": True, "on": None,
                "text": "不知道%s%s现在开着没有" % (room_zh, dev_zh)}
    return {"ok": True, "on": state,
            "text": "%s现在%s着" % (room_zh + dev_zh, "开" if state else "关")}


def tool_run_scene(name: str, room: str = "none") -> dict:
    """Run a named scene (sleep/home/away/movie/party/read)."""
    scene = _SCENES.get(name) or (_SCENES.get(name.replace("scene_", ""))
                                  if isinstance(name, str) else None)
    if scene is None:
        return {"ok": False,
                "error": "没有这个场景：%s。可选：%s" % (name, "、".join(_SCENES))}
    if room not in _VALID_ROOMS:
        return {"ok": False, "error": "不认识的房间：%s" % room}
    cmds = scenes.expand(scene, room)
    if not cmds:
        return {"ok": False, "error": "场景 %s 展开失败" % name}
    done, failed = [], []
    for cmd in cmds:
        r = _controller().execute(cmd["action"], cmd["room"])
        if r.get("ok"):
            done.append(cmd["action"])
        else:
            failed.append(cmd["action"])
    if failed:
        return {"ok": False,
                "error": "场景部分没成：%s" % "、".join(failed),
                "executed": done}
    return {"ok": True, "executed": done,
            "text": "%s场景已执行" % name.replace("scene_", "")}


def tool_list_rooms() -> dict:
    """List rooms and the devices each has."""
    from home_config import DEVICE_ROOMS
    lines = []
    for room in ROOMS:
        devs = [DEVICE_ZH[d] for d, rs in DEVICE_ROOMS.items() if room in rs]
        lines.append("%s（%s）" % (ROOM_ZH[room], "、".join(devs) or "无设备"))
    return {"ok": True, "text": "房间有：" + "；".join(lines)}


_TOOLS = [
    {"name": "set_device",
     "description": "开关设备。device: light/ac/fan/tv/curtain；room: 客厅/卧室/厨房/卫生间/书房/阳台/餐厅/儿童房/all",
     "inputSchema": {
         "type": "object",
         "properties": {
             "device": {"type": "string", "enum": list(_VALID_DEVICES)},
             "on": {"type": "boolean"},
             "room": {"type": "string", "enum": list(_VALID_ROOMS)},
         },
         "required": ["device", "on", "room"],
     }},
    {"name": "query_device",
     "description": "查询设备开着还是关着",
     "inputSchema": {
         "type": "object",
         "properties": {
             "device": {"type": "string", "enum": list(_VALID_DEVICES)},
             "room": {"type": "string", "enum": list(_VALID_ROOMS)},
         },
         "required": ["device", "room"],
     }},
    {"name": "run_scene",
     "description": "执行场景：sleep/home/away/movie/party/read",
     "inputSchema": {
         "type": "object",
         "properties": {
             "name": {"type": "string", "enum": list(_SCENES)},
             "room": {"type": "string", "enum": list(_VALID_ROOMS)},
         },
         "required": ["name"],
     }},
    {"name": "list_rooms",
     "description": "列出房间和每个房间有的设备",
     "inputSchema": {"type": "object", "properties": {}}},
]

_DISPATCH = {
    "set_device": lambda a: tool_set_device(
        a.get("device"), a.get("on"), a.get("room")),
    "query_device": lambda a: tool_query_device(
        a.get("device"), a.get("room")),
    "run_scene": lambda a: tool_run_scene(
        a.get("name"), a.get("room", "none")),
    "list_rooms": lambda a: tool_list_rooms(),
}


# ---------------------------------------------------------------------------
# JSON-RPC / MCP plumbing
# ---------------------------------------------------------------------------

def _tool_result(result: dict) -> dict:
    """Tool dict -> MCP content response. Errors are text + isError flag."""
    if "error" in result:
        return {"content": [{"type": "text", "text": result["error"]}],
                "isError": True}
    return {"content": [{"type": "text", "text": result.get("text", json.dumps(
        result, ensure_ascii=False))}], "isError": False}


def handle_request(req: dict):
    """One JSON-RPC request -> response dict, or None for notifications."""
    method = req.get("method", "")
    rid = req.get("id")
    if method == "initialize":
        result = {
            "protocolVersion": req.get("params", {}).get(
                "protocolVersion", "2025-03-26"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "home-mcp", "version": "1.0"},
        }
    elif method == "tools/list":
        result = {"tools": _TOOLS}
    elif method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        fn = _DISPATCH.get(name)
        if fn is None:
            result = {"content": [{"type": "text",
                                   "text": "没有这个工具：%s" % name}],
                      "isError": True}
        else:
            try:
                result = _tool_result(fn(params.get("arguments") or {}))
            except Exception as e:  # tool crash must not kill the server
                result = {"content": [{"type": "text", "text": "工具出错：%s" % e}],
                          "isError": True}
    elif method.startswith("notifications/"):
        return None
    elif rid is None:
        return None
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {
            "code": -32601, "message": "method not found: %s" % method}}
    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def serve_lines(lines) -> list:
    """Process request JSON strings; return response JSON strings."""
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            out.append(json.dumps({"jsonrpc": "2.0", "id": None, "error": {
                "code": -32700, "message": "parse error"}}, ensure_ascii=False))
            continue
        resp = handle_request(req)
        if resp is not None:
            out.append(json.dumps(resp, ensure_ascii=False))
    return out


def main() -> None:
    """stdio server loop for mcp_pool to spawn."""
    for line in sys.stdin:
        for out in serve_lines([line]):
            sys.stdout.write(out + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
