"""发 MQTT：status online + play <url> + dump。用法: python mqplay.py <url|dump>"""
import sys, time
import paho.mqtt.client as mqtt
try:
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
except AttributeError:
    c = mqtt.Client()
c.connect("192.168.207.150", 1883, 30)
c.loop_start()
time.sleep(1)
c.publish("xiaochen-node1/status", "online").wait_for_publish()
time.sleep(2)
arg = sys.argv[1] if len(sys.argv) > 1 else "dump"
if arg == "dump":
    c.publish("chenvoice/control/node1/dump", "").wait_for_publish()
    time.sleep(1)
    c.publish("chenvoice/control/node1/dump", "").wait_for_publish()
    print("dump x2")
else:
    c.publish("chenvoice/control/node1/play", arg).wait_for_publish()
    print("play", arg)
time.sleep(0.5)
c.loop_stop()
