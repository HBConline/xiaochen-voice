# -*- coding: utf-8 -*-
"""PC-side ASR (SenseVoice, same model as the board) — this is the "listening".

Transcribes every unit wav, plus the long merged spans sliced into 1s hops,
so the cutter can see exactly where each wake phrase sits.
"""
import glob
import os
import sys

import numpy as np
import sherpa_onnx
import soundfile as sf

sys.stdout.reconfigure(encoding="utf-8")
APP = r"D:\chenvoice-deploy\app"
UNIT_DIR = r"D:\mww\tmp\units"
RAW = r"D:\mww\tmp\real_raw.wav"

rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=os.path.join(APP, "model.int8.onnx"),
    tokens=os.path.join(APP, "tokens.txt"),
    language="zh",
    use_itn=True,
    num_threads=2,
    provider="cpu",
)


def transcribe(audio, sr):
    stream = rec.create_stream()
    stream.accept_waveform(sr, audio.astype(np.float32))
    rec.decode_stream(stream)
    return stream.result.text.strip()


print("=== units ===", flush=True)
for path in sorted(glob.glob(os.path.join(UNIT_DIR, "*.wav"))):
    audio, sr = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    print("%s  %.2fs  %s" % (os.path.basename(path), len(audio) / sr,
                             transcribe(audio, sr)), flush=True)

# long merged spans from segment_v2: 3.3-9.56s. Slice 1.2s hops to locate phrases.
print("=== span 3.3-9.6s, 1.2s hop ===", flush=True)
audio, sr = sf.read(RAW, dtype="float32")
if audio.ndim > 1:
    audio = audio.mean(axis=1)
t0, t1 = 3.3, 9.6
hop = 1.2
t = t0
while t < t1:
    seg = audio[int(t * sr): int(min(t + hop + 0.3, t1) * sr)]
    if len(seg) / sr >= 0.4:
        print("t=%.1f-%.1f  %s" % (t, t + hop, transcribe(seg, sr)), flush=True)
    t += hop

print("=== span 45.8-50.4s, 1.2s hop ===", flush=True)
t0, t1 = 45.8, 50.4
t = t0
while t < t1:
    seg = audio[int(t * sr): int(min(t + hop + 0.3, t1) * sr)]
    if len(seg) / sr >= 0.4:
        print("t=%.1f-%.1f  %s" % (t, t + hop, transcribe(seg, sr)), flush=True)
    t += hop
