# -*- coding: utf-8 -*-
"""Scan 192.168.207.0/24 for the fan Tasmota module (MAC ec:fa:bc)."""
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor

RANGE = ["192.168.207.%d" % i for i in range(2, 255)]
WANT = "ec:fa:bc"


def probe(ip):
    try:
        with urllib.request.urlopen(
            "http://%s/cm?cmnd=Status%%205" % ip, timeout=1.2
        ) as r:
            body = r.read(2048).decode("utf8", "replace")
        mac = json.loads(body).get("StatusNET", {}).get("Mac", "")
        return ip, mac, body
    except Exception:
        return ip, None, None


hits = []
with ThreadPoolExecutor(max_workers=64) as pool:
    for ip, mac, body in pool.map(probe, RANGE):
        if mac:
            tag = " <-- FAN" if mac.lower().startswith(WANT) else ""
            print("%s mac=%s%s" % (ip, mac, tag), flush=True)
            if mac.lower().startswith(WANT):
                hits.append(ip)

print("[fan ip]", hits or "NOT FOUND")
