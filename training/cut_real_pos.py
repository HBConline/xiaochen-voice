# -*- coding: utf-8 -*-
"""Cut a real-voice recording into wake-word clips for pos_real training.

Energy-based segmentation on the 16k mono copy:
  frame RMS -> speech mask -> merge gaps < 300ms -> pad 0.15s
  keep regions 0.5-2.5s (spoken 小宸同学 ~1s), write s16 16k mono wav.
"""
import glob
import os
import subprocess
import sys
import wave

import numpy as np

SRC = [sys.argv[1]] if len(sys.argv) > 1 else glob.glob(r"C:\Users\HBC\Desktop\2026*.m4a")
OUT = r"D:\mww\samples\pos_real"
TMP = r"D:\mww\tmp\real_raw.wav"
PREFIX = os.environ.get("CUT_PREFIX", "r9_p")
FRAME_MS = 20
THRESH = 0.01          # RMS / full-scale; phone mic shout is far above
MERGE_GAP_S = 0.30
PAD_S = 0.15
MIN_S, MAX_S = 0.5, 2.5

os.makedirs(OUT, exist_ok=True)
os.makedirs(os.path.dirname(TMP), exist_ok=True)
assert SRC, "no 2026*.m4a on Desktop"
print("[src]", SRC[0], flush=True)

subprocess.run(
    ["ffmpeg", "-y", "-loglevel", "error", "-i", SRC[0],
     "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", TMP], check=True)

with wave.open(TMP, "rb") as w:
    sr = w.getframerate()
    x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
print("[wav] %d samples %.2fs" % (len(x), len(x) / sr), flush=True)

frame = sr * FRAME_MS // 1000
n = len(x) // frame
rms = np.sqrt((x[: n * frame].reshape(n, frame) ** 2).mean(axis=1))
speech = rms > THRESH

# merge short gaps: any False run < MERGE_GAP_S surrounded by True becomes True
gap_frames = int(MERGE_GAP_S * 1000 / FRAME_MS)
regions = []
i = 0
while i < n:
    if not speech[i]:
        i += 1
        continue
    j = i
    while j < n:
        if speech[j]:
            j += 1
        else:
            k = j
            while k < n and not speech[k]:
                k += 1
            if k - j < gap_frames and k < n:
                j = k
            else:
                break
    regions.append((i * frame, j * frame))
    i = j

clips = []
for a, b in regions:
    a = max(0, a - int(PAD_S * sr))
    b = min(len(x), b + int(PAD_S * sr))
    dur = (b - a) / sr
    if MIN_S <= dur <= MAX_S:
        clips.append((a, b, dur))
    else:
        print("[skip] dur=%.2fs at %.1fs" % (dur, a / sr), flush=True)

for idx, (a, b, dur) in enumerate(clips):
    path = os.path.join(OUT, "%s%03d.wav" % (PREFIX, idx))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((x[a:b] * 32767).astype("<i2").tobytes())
    peak = float(np.abs(x[a:b]).max())
    print("[cut] %s  %.2fs  peak=%.3f" % (path, dur, peak), flush=True)

print("[done] %d clips -> %s" % (len(clips), OUT), flush=True)
sys.exit(0 if clips else 1)
