# -*- coding: utf-8 -*-
"""Tests for MqttRelayBackend topic override (Tasmota relay integration).

The physical relay is a Tasmota ESP-01 speaking cmnd/<topic>/POWER on/off,
while the Phase-1 contract is chenvoice/<room>/set. A per-room topic map
bridges the two without breaking existing users.

Run: PYTHONUTF8=1 python test_mqtt_relay.py  (exit code 0 = all pass)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "servers"))
sys.stdout.reconfigure(encoding="utf-8")

os.environ["DEVICE_BACKEND"] = "mock"
os.environ["DEVICE_STATE_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="mqtt_relay_test_"), "state.json"
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


class FakeClient:
    """Stands in for paho.mqtt.client: records publishes, always succeeds."""

    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

        class Info:
            def wait_for_publish(self, timeout=None):
                pass

            def is_published(self):
                return True

        return Info()


TOPICS = {"living_room": "cmnd/xiaochen-relay1/POWER"}

# --- topic map ---------------------------------------------------------------

fc = FakeClient()
b = device_layer.MqttRelayBackend(topics=TOPICS, client=fc)
b.set_state("living_room", True)
check("override_topic_on",
      fc.published == [("cmnd/xiaochen-relay1/POWER", "on", 1, True)], fc.published)

b.set_state("living_room", False)
check("override_topic_off",
      fc.published[-1] == ("cmnd/xiaochen-relay1/POWER", "off", 1, True), fc.published)

fc2 = FakeClient()
b2 = device_layer.MqttRelayBackend(client=fc2)
b2.set_state("bedroom", True)
check("default_topic_unmapped_room",
      fc2.published == [("chenvoice/bedroom/set", "on", 1, True)], fc2.published)

fc3 = FakeClient()
b3 = device_layer.MqttRelayBackend(topics=TOPICS, client=fc3)
b3.set_state("bedroom", True)
check("override_map_misses_falls_back",
      fc3.published == [("chenvoice/bedroom/set", "on", 1, True)], fc3.published)

# --- env parsing -------------------------------------------------------------

check("parse_topics",
      device_layer.parse_relay_topics("living_room=cmnd/xiaochen-relay1/POWER")
      == TOPICS)
check("parse_topics_multi",
      device_layer.parse_relay_topics(
          "living_room=cmnd/a/POWER,bedroom=cmnd/b/POWER")
      == {"living_room": "cmnd/a/POWER", "bedroom": "cmnd/b/POWER"})
check("parse_topics_empty", device_layer.parse_relay_topics("") == {})
check("parse_topics_none", device_layer.parse_relay_topics(None) == {})

# --- voice tool path end to end ---------------------------------------------

fc4 = FakeClient()
ctl = device_layer.DeviceController(
    device_layer.MqttRelayBackend(topics=TOPICS, client=fc4))
home_mcp.set_controller(ctl)
r = home_mcp.tool_set_device("light", True, "living_room")
check("voice_set_living_light_on", r.get("ok") and r.get("text") == "客厅灯已开", r)
check("voice_set_publishes_tasmota_topic",
      fc4.published[-1][0] == "cmnd/xiaochen-relay1/POWER" and
      fc4.published[-1][1] == "on", fc4.published)

r = home_mcp.tool_set_device("light", False, "living_room")
check("voice_set_living_light_off", r.get("text") == "客厅灯已关", r)

r = home_mcp.tool_query_device("light", "living_room")
check("voice_query_after_off", r.get("on") is False, r)

print("%d checks, %d fail" % (N[0], len(FAILS)))
if FAILS:
    print("FAILED:", ", ".join(FAILS))
sys.exit(1 if FAILS else 0)
