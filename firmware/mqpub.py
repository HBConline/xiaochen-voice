import sys,time
import paho.mqtt.client as mqtt
try:
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
except AttributeError:
    c = mqtt.Client()
c.connect("192.168.207.150", 1883, 30)
c.loop_start()
r = c.publish(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
r.wait_for_publish()
time.sleep(0.5)
print("sent", sys.argv[1], "rc=", r.rc)
c.loop_stop()
