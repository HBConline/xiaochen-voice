# -*- coding: utf-8 -*-
"""Transcribe every unit on the OrangePi SenseVoice and print the table.

POST to wakeword_server /transcribe (ASR text = our ears). No device side
effects on this path: intent is parsed but nothing executes.
"""
import io
import json
import os
import sys
import urllib.request
import wave

sys.stdout.reconfigure(encoding="utf-8")
UNIT_DIR = r"D:\mww\tmp\units"
URL = "http://192.168.207.150:8899/transcribe"

for name in sorted(os.listdir(UNIT_DIR)):
    if not name.endswith(".wav"):
        continue
    path = os.path.join(UNIT_DIR, name)
    with wave.open(path, "rb") as w:
        dur = w.getnframes() / float(w.getframerate())
    with open(path, "rb") as f:
        data = f.read()
    boundary = "----cutbound"
    body = (
        ("--%s\r\n" % boundary).encode()
        + ('Content-Disposition: form-data; name="audio"; filename="%s"\r\n' % name).encode()
        + b"Content-Type: audio/wav\r\n\r\n"
        + data
        + ("\r\n--%s--\r\n" % boundary).encode()
    )
    req = urllib.request.Request(URL, data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=%s" % boundary)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.load(r)
        text = resp.get("text", "")
    except Exception as e:
        text = "ERR:%s" % e
    print("%s  %.2fs  %s" % (name, dur, text), flush=True)
