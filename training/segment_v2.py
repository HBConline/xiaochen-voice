# -*- coding: utf-8 -*-
"""Segment the real recording into speech units, finer gap split.

Merge gap 0.12s: separates back-to-back shouts, keeps one utterance
"小宸同学" whole (intra-word dips are shorter). Units 0.4-3.5s go to
D:\\mww\\tmp\\units\\ for ASR verification; nothing is claimed positive yet.
"""
import glob
import os
import subprocess
import wave

import numpy as np

SRC = glob.glob(r"C:\Users\HBC\Desktop\2026*.m4a")[0]
OUT = r"D:\mww\tmp\units"
TMP = r"D:\mww\tmp\real_raw.wav"
FRAME_MS = 20
THRESH = 0.01
MERGE_GAP_S = 0.12
PAD_S = 0.12
MIN_S, MAX_S = 0.4, 3.5

os.makedirs(OUT, exist_ok=True)
for f in os.listdir(OUT):
    os.remove(os.path.join(OUT, f))

if not os.path.exists(TMP):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", SRC,
                    "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", TMP], check=True)

with wave.open(TMP, "rb") as w:
    sr = w.getframerate()
    x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0

frame = sr * FRAME_MS // 1000
n = len(x) // frame
rms = np.sqrt((x[: n * frame].reshape(n, frame) ** 2).mean(axis=1))
speech = rms > THRESH

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

idx = 0
for a, b in regions:
    a2 = max(0, a - int(PAD_S * sr))
    b2 = min(len(x), b + int(PAD_S * sr))
    dur = (b2 - a2) / sr
    if not (MIN_S <= dur <= MAX_S):
        print("[skip] %.2fs at %.1fs" % (dur, a / sr), flush=True)
        continue
    path = os.path.join(OUT, "u%02d.wav" % idx)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((x[a2:b2] * 32767).astype("<i2").tobytes())
    print("[unit] %s %.2fs t=%.2f-%.2f peak=%.2f"
          % (path, dur, a2 / sr, b2 / sr, float(np.abs(x[a2:b2]).max())), flush=True)
    idx += 1

print("[done] %d units" % idx, flush=True)
