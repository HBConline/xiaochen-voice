# -*- coding: utf-8 -*-
"""OTA flash xiaochen-node1 via esphome espota2 library (port 3232, no auth)."""
import sys

from esphome import espota2

FW = r"D:\esphome-xiaochen\.esphome\build\xiaochen-node1\.pio\build\xiaochen-node1\firmware.bin"
print("[ota] target 192.168.207.224:3232", FW, flush=True)
espota2.run_ota("192.168.207.224", 3232, "", FW)
print("[ota] run_ota returned", flush=True)
