# -*- coding: utf-8 -*-
"""Transcribe neg_nearmiss_real clips to verify labels (hot ones especially)."""
import glob
import os
import sys
import wave

import numpy as np
import sherpa_onnx

sys.stdout.reconfigure(encoding="utf-8")
APP = r"D:\chenvoice-deploy\app"

rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=os.path.join(APP, "model.int8.onnx"),
    tokens=os.path.join(APP, "tokens.txt"),
    language="zh", use_itn=True, num_threads=2, provider="cpu")

for f in sorted(glob.glob(r"D:\mww\samples\neg_nearmiss_real\*.wav")):
    with wave.open(f, "rb") as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    st = rec.create_stream()
    st.accept_waveform(sr, x)
    rec.decode_stream(st)
    print("%s  %s" % (os.path.basename(f), st.result.text.strip()), flush=True)
