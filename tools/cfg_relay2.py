# -*- coding: utf-8 -*-
"""Configure bedroom ESP-01 relay (Tasmota) over serial COM port.

Reads wifi_ssid/wifi_password from D:\\esphome-xiaochen\\secrets.yaml.
Never prints the password - all serial output is redacted before printing.
"""
import re
import sys
import time

import serial
import serial.tools.list_ports

SECRETS = r"D:\esphome-xiaochen\secrets.yaml"
TEMPLATE = '{"NAME":"ESP01-Relay","GPIO":[256,0,0,0,0,0,0,0],"FLAG":0,"BASE":18}'
TOPIC = "xiaochen-relay2"  # argv[2] overrides, e.g. xiaochen-curtain1
MQTT_HOST = "192.168.207.150"


def load_wifi():
    text = open(SECRETS, encoding="utf8").read()
    ssid = re.search(r"^wifi_ssid:\s*(.+)$", text, re.M).group(1).strip().strip("'\"")
    pw = re.search(r"^wifi_password:\s*(.+)$", text, re.M).group(1).strip().strip("'\"")
    return ssid, pw


def find_port():
    for p in serial.tools.list_ports.comports():
        if "CH340" in (p.description or "") or "USB-SERIAL" in (p.description or ""):
            return p.device
    return "COM5"


def send(ser, cmd, wait=1.2, redact=None):
    ser.reset_input_buffer()
    ser.write((cmd + "\r\n").encode("utf8"))
    ser.flush()
    time.sleep(wait)
    out = ser.read(ser.in_waiting or 4096).decode("utf8", errors="replace")
    if redact:
        out = out.replace(redact, "***")
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if redact and redact in line:
            continue  # never echo lines that carried the password
        if "Password1" in line:
            continue
        print("  <", line)
    return out


def main():
    ssid, pw = load_wifi()
    port = sys.argv[1] if len(sys.argv) > 1 else find_port()
    topic = sys.argv[2] if len(sys.argv) > 2 else TOPIC
    print("[serial]", port, "[topic]", topic, "(password hidden)")
    ser = serial.Serial(port, 115200, timeout=1)
    time.sleep(0.5)
    boot = ser.read(ser.in_waiting or 4096).decode("utf8", errors="replace")
    print("[boot banner]", "*** %d bytes ***" % len(boot) if boot else "(none)")

    print("[1] wifi + mqtt + topic")
    send(ser, "Backlog SSID1 %s; Password1 %s; MqttHost %s; MqttPort 1883; Topic %s"
         % (ssid, pw, MQTT_HOST, topic), wait=4, redact=pw)

    print("[2] template")
    send(ser, "Template %s" % TEMPLATE, wait=2, redact=pw)

    print("[3] Module 0 (apply template, restart)")
    send(ser, "Module 0", wait=2, redact=pw)

    print("[4] wait boot + wifi connect")
    time.sleep(15)
    out = ser.read(ser.in_waiting or 65536).decode("utf8", errors="replace")
    out = out.replace(pw, "***")
    for line in out.splitlines():
        if line.strip() and "Password" not in line:
            print("  <", line.strip())

    print("[5] Status 5 (network)")
    send(ser, "Status 5", wait=2, redact=pw)
    print("[6] Status 11 (mqtt)")
    send(ser, "Status 11", wait=2, redact=pw)
    ser.close()
    print("[done]")


if __name__ == "__main__":
    main()
