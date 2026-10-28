# -*- coding: utf-8 -*-
"""Curtain hardware binding for the MQTT relay backend.

The backend topic map is keyed by room (light). A curtain is one appliance,
so it binds through device-scoped keys instead:
    curtain=cmnd/xiaochen-curtain1/POWER            -> roomless "打开窗帘"
    curtain:living_room=cmnd/xiaochen-curtain1/POWER -> "打开客厅窗帘"
A room that has no device-scoped key stays state-only (打开卧室窗帘 with only
a living-room curtain must not drive the living-room hardware).

Run: PYTHONUTF8=1 python test_curtain_relay.py  (exit code 0 = all pass)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

os.environ["DEVICE_BACKEND"] = "mock"
os.environ["DEVICE_STATE_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="curtain_test_"), "state.json"
)

import device_layer

FAILS = []
N = [0]


def check(name, cond, detail=""):
    N[0] += 1
    status = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print("[%s] %s %s" % (status, name, detail if not cond else ""))


class FakeInfo:
    def wait_for_publish(self, timeout=None):
        pass

    def is_published(self):
        return True


class FakeClient:
    def __init__(self):
        self.pubs = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.pubs.append((topic, payload))
        return FakeInfo()


def ctl_with(topics):
    client = FakeClient()
    backend = device_layer.MqttRelayBackend(topics=topics, client=client)
    return device_layer.DeviceController(backend), client


# parse keeps opaque device-scoped keys
d = device_layer.parse_relay_topics(
    "curtain=cmnd/xiaochen-curtain1/POWER,curtain:living_room=cmnd/xiaochen-curtain1/POWER"
)
check("parse_curtain_key", d.get("curtain") == "cmnd/xiaochen-curtain1/POWER", d)
check("parse_curtain_lr_key",
      d.get("curtain:living_room") == "cmnd/xiaochen-curtain1/POWER", d)

# roomless open/close drive the generic curtain binding
ctl, client = ctl_with({"curtain": "cmnd/xiaochen-curtain1/POWER"})
r = ctl.execute("curtain_open", "none")
check("curtain_open_none_ok", r["ok"] and r.get("executed"), r)
check("curtain_open_none_pub",
      client.pubs == [("cmnd/xiaochen-curtain1/POWER", "on")], client.pubs)

ctl, client = ctl_with({"curtain": "cmnd/xiaochen-curtain1/POWER"})
r = ctl.execute("curtain_close", "none")
check("curtain_close_none_pub",
      client.pubs == [("cmnd/xiaochen-curtain1/POWER", "off")], client.pubs)

# room-qualified command binds curtain:<room>
ctl, client = ctl_with({"curtain:living_room": "cmnd/xiaochen-curtain1/POWER"})
r = ctl.execute("curtain_open", "living_room")
check("curtain_open_lr_ok", r["ok"] and r.get("executed"), r)
check("curtain_open_lr_pub",
      client.pubs == [("cmnd/xiaochen-curtain1/POWER", "on")], client.pubs)

# bedroom has no binding: state-only, nothing published
ctl, client = ctl_with({"curtain": "cmnd/xiaochen-curtain1/POWER"})
r = ctl.execute("curtain_open", "bedroom")
check("curtain_bedroom_no_pub", not client.pubs, client.pubs)
check("curtain_bedroom_state_only", "state-only" in (r.get("detail") or ""), r)

# light regression: room-keyed bindings unchanged
ctl, client = ctl_with({"living_room": "cmnd/xiaochen-relay1/POWER"})
r = ctl.execute("light_on", "living_room")
check("light_lr_pub",
      client.pubs == [("cmnd/xiaochen-relay1/POWER", "on")], client.pubs)

ctl, client = ctl_with({"curtain": "cmnd/xiaochen-curtain1/POWER"})
r = ctl.execute("light_on", "none")
check("light_none_state_only", not client.pubs, (client.pubs, r))

print("== %d checks, %d failed ==" % (N[0], len(FAILS)))
sys.exit(1 if FAILS else 0)
