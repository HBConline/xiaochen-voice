# -*- coding: utf-8 -*-
"""Recover near-miss negatives that process_nearmiss dropped or merged.

1. Re-keep any dropped segment that has a 小X同... shape but isn't the wake name
   (e.g. ASR '小张同情' for 小张同寝).
2. Sub-split over-long merged regions by energy valleys and ASR each piece;
   keep every non-wake piece that contains 同.
"""
import os
import re
import sys
import wave

import numpy as np
import sherpa_onnx
import soundfile as sf

sys.stdout.reconfigure(encoding="utf-8")
APP = r"D:\chenvoice-deploy\app"
OUT = r"D:\mww\samples\neg_nearmiss_real"
TMP = r"D:\mww\tmp\nearmiss_raw.wav"

FRAME_MS = 20
THRESH = 0.01
MERGE_GAP_S = 0.12
PAD_S = 0.12
MIN_S, MAX_S = 0.6, 4.0
VALLEY_RATIO = 0.15

WAKE_RE = re.compile(r"小\s*[宸陈辰晨尘成]")
KEEP_RE = re.compile(r"同|铜|童")

audio, sr = sf.read(TMP, dtype="float32")
if audio.ndim > 1:
    audio = audio.mean(axis=1)

rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=os.path.join(APP, "model.int8.onnx"),
    tokens=os.path.join(APP, "tokens.txt"),
    language="zh", use_itn=True, num_threads=2, provider="cpu")


def transcribe(seg):
    st = rec.create_stream()
    st.accept_waveform(sr, seg.astype(np.float32))
    rec.decode_stream(st)
    return st.result.text.strip()


frame = sr * FRAME_MS // 1000
n = len(audio) // frame
rms = np.sqrt((audio[: n * frame].reshape(n, frame) ** 2).mean(axis=1))
speech = rms > THRESH

# fine segmentation (0.12s gap) so merged runs split into phrases
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

kept = 0
existing = len([f for f in os.listdir(OUT) if f.endswith(".wav")])
for a, b in regions:
    dur = (b - a) / sr
    pieces = [(a, b)]
    if dur > MAX_S:
        # valley-split the long run
        lo_i, hi_i = a // frame, b // frame
        med = float(np.median(rms[lo_i:hi_i][rms[lo_i:hi_i] > THRESH])) or 0.05
        valleys = rms[lo_i:hi_i] < VALLEY_RATIO * med
        cuts = []
        v = 0
        while v < len(valleys):
            if valleys[v]:
                w = v
                while w < len(valleys) and valleys[w]:
                    w += 1
                cuts.append(lo_i + (v + w) // 2)
                v = w
            else:
                v += 1
        bounds = [lo_i] + cuts + [hi_i]
        pieces = []
        for p, q in zip(bounds[:-1], bounds[1:]):
            if (q - p) * frame / sr >= MIN_S:
                pieces.append((p * frame, q * frame))
    for a2, b2 in pieces:
        a3 = max(0, a2 - int(PAD_S * sr))
        b3 = min(len(audio), b2 + int(PAD_S * sr))
        d = (b3 - a3) / sr
        if not (MIN_S <= d <= MAX_S):
            print("[skip] %.2fs" % d, flush=True)
            continue
        seg = audio[a3:b3]
        text = transcribe(seg)
        if WAKE_RE.search(text):
            print("[drop-wake] %.2fs  %s" % (d, text), flush=True)
            continue
        if not KEEP_RE.search(text):
            print("[drop-off] %.2fs  %s" % (d, text), flush=True)
            continue
        path = os.path.join(OUT, "nm%03d.wav" % (existing + kept))
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes((seg * 32767).astype("<i2").tobytes())
        print("[keep] %s %.2fs t=%.1f-%.1f  %s"
              % (os.path.basename(path), d, a3 / sr, b3 / sr, text), flush=True)
        kept += 1

print("[done] recovered %d -> total %d" % (kept, existing + kept), flush=True)
