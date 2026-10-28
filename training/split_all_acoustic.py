# -*- coding: utf-8 -*-
"""Acoustic-only split of real_raw.wav into wake takes. No ASR gate.

User rule: every utterance in this recording IS 小宸同学. Keep all speech
pieces; just cut glued fast-read runs at deep valleys. Target piece length
0.7-2.2s (phrase ~1-1.5s). Overlong pieces force-cut at deepest internal
valley; short dust dropped.
"""
import os
import sys
import wave

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
RAW = r"D:\mww\tmp\real_raw.wav"
OUT = r"D:\mww\samples\pos_real"
PREFIX = "r9_p"
FRAME_MS = 10
SPEECH_THR = 0.01
MERGE_GAP_S = 0.12        # fast-read gaps are tiny; only bridge breaths
PAD_S = 0.12
MIN_S, MAX_S = 0.7, 2.5
VALLEY_RATIO = 0.20

with wave.open(RAW, "rb") as w:
    sr = w.getframerate()
    x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
print("[wav] %.2fs @%d" % (len(x) / sr, sr), flush=True)

frame = sr * FRAME_MS // 1000
n_total = len(x) // frame
rms = np.sqrt((x[: n_total * frame].reshape(n_total, frame) ** 2).mean(axis=1))
speech = rms > SPEECH_THR
gap_frames = int(MERGE_GAP_S * 1000 / FRAME_MS)

regions = []
i = 0
while i < n_total:
    if not speech[i]:
        i += 1
        continue
    j = i
    while j < n_total:
        if speech[j]:
            j += 1
        else:
            k = j
            while k < n_total and not speech[k]:
                k += 1
            if k - j < gap_frames and k < n_total:
                j = k
            else:
                break
    regions.append([i, j])
    i = j
print("[regions] %d" % len(regions), flush=True)


def valley_split(lo, hi):
    if (hi - lo) * FRAME_MS / 1000 <= MAX_S:
        return [(lo, hi)]
    seg = rms[lo:hi]
    active = seg[seg > SPEECH_THR]
    med = float(np.median(active)) if len(active) else 0.05
    valleys = seg < VALLEY_RATIO * med
    runs, t = [], 0
    while t < len(valleys):
        if valleys[t]:
            u = t
            while u < len(valleys) and valleys[u]:
                u += 1
            if t > 0 and u < len(valleys):
                runs.append((u - t, (t + u) // 2))
            t = u
        else:
            t += 1
    if not runs:
        mid = seg[5:-5]
        cut = lo + 5 + int(np.argmin(mid)) if len(mid) else (lo + hi) // 2
    else:
        cut = lo + max(runs, key=lambda r: r[0])[1]
    return valley_split(lo, cut) + valley_split(cut, hi)


pieces = []
for lo, hi in regions:
    pieces.extend(valley_split(lo, hi))

for f in os.listdir(OUT):
    if f.startswith(PREFIX):
        os.remove(os.path.join(OUT, f))

kept = 0
for lo, hi in pieces:
    a = max(0, lo * frame - int(PAD_S * sr))
    b = min(len(x), hi * frame + int(PAD_S * sr))
    seg = x[a:b]
    dur = len(seg) / sr
    peak = float(np.abs(seg).max())
    if not (MIN_S <= dur <= MAX_S) or peak < 0.08:
        print("[drop] %.2fs t=%.1f peak=%.2f" % (dur, a / sr, peak), flush=True)
        continue
    path = os.path.join(OUT, "%s%03d.wav" % (PREFIX, kept))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((seg * 32767).astype("<i2").tobytes())
    print("[keep] %s %.2fs t=%.1f peak=%.2f" % (os.path.basename(path), dur, a / sr, peak), flush=True)
    kept += 1

print("[done] kept %d / %d pieces -> %s" % (kept, len(pieces), OUT), flush=True)
