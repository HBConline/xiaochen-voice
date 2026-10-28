# -*- coding: utf-8 -*-
"""Valley-split ALL speech regions of real_raw.wav into wake takes, ASR-gated.

Energy regions glue fast-read phrases together (gaps < 300ms). For any region
> 2.2s, cut at deep valleys (< VALLEY_RATIO of region speech-median) recursively
until every piece <= 2.2s. Each piece goes through local SenseVoice; only text
matching the wake phrase (宸/陈 ASR bias included) is kept in pos_real.
"""
import os
import re
import sys
import wave

import numpy as np
import sherpa_onnx

sys.stdout.reconfigure(encoding="utf-8")
RAW = r"D:\mww\tmp\real_raw.wav"
OUT = r"D:\mww\samples\pos_real"
APP = r"D:\chenvoice-deploy\app"
PREFIX = "r9_p"
FRAME_MS = 10
SPEECH_THR = 0.01
MERGE_GAP_S = 0.30
PAD_S = 0.12
MIN_S, MAX_S = 0.5, 2.5
MAX_PIECE_S = 2.2
VALLEY_RATIO = 0.15
# SenseVoice hears 宸 as 陈; accept both, also tolerate 学/穴
WAKE_RE = re.compile(r"小[宸陈沈][同童][学穴]")

with wave.open(RAW, "rb") as w:
    sr = w.getframerate()
    x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
print("[wav] %.2fs @%d" % (len(x) / sr, sr), flush=True)

frame = sr * FRAME_MS // 1000
n_total = len(x) // frame


def env_rms(seg):
    m = len(seg) // frame
    return np.sqrt((seg[: m * frame].reshape(m, frame) ** 2).mean(axis=1))


rms = env_rms(x)
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
            while k < n_total and speech[k]:
                k += 1
            if k - j < gap_frames and k < n_total:
                j = k
            else:
                break
    regions.append([i, j])
    i = j


def valley_split(lo, hi):
    """Recursively split [lo,hi) frame range until each piece <= MAX_PIECE_S."""
    if (hi - lo) * FRAME_MS / 1000 <= MAX_PIECE_S:
        return [(lo, hi)]
    seg_rms = rms[lo:hi]
    active = seg_rms[seg_rms > SPEECH_THR]
    med = float(np.median(active)) if len(active) else 0.05
    valleys = seg_rms < VALLEY_RATIO * med
    runs = []
    t = 0
    while t < len(valleys):
        if valleys[t]:
            u = t
            while u < len(valleys) and valleys[u]:
                u += 1
            if 0 < t and u < len(valleys):
                runs.append((u - t, (t + u) // 2))
            t = u
        else:
            t += 1
    if not runs:
        cut = lo + 5 + int(np.argmin(rms[lo + 5: hi - 5])) if hi - lo > 10 else (lo + hi) // 2
    else:
        cut = lo + max(runs, key=lambda r: r[0])[1]
    return valley_split(lo, cut) + valley_split(cut, hi)


pieces = []
for lo, hi in regions:
    pieces.extend(valley_split(lo, hi))

rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=os.path.join(APP, "model.int8.onnx"),
    tokens=os.path.join(APP, "tokens.txt"),
    language="zh", use_itn=True, num_threads=2, provider="cpu")


def transcribe(seg):
    st = rec.create_stream()
    st.accept_waveform(sr, seg)
    rec.decode_stream(st)
    return st.result.text.strip()


for f in os.listdir(OUT):
    if f.startswith(PREFIX):
        os.remove(os.path.join(OUT, f))

kept = 0
for lo, hi in pieces:
    a = max(0, lo * frame - int(PAD_S * sr))
    b = min(len(x), hi * frame + int(PAD_S * sr))
    seg = x[a:b]
    dur = len(seg) / sr
    if not (MIN_S <= dur <= MAX_S):
        print("[drop] %.2fs t=%.1f dur out of range" % (dur, a / sr), flush=True)
        continue
    peak = float(np.abs(seg).max())
    if peak < 0.08:
        print("[drop] %.2fs t=%.1f too quiet peak=%.3f" % (dur, a / sr, peak), flush=True)
        continue
    text = transcribe(seg)
    ok = bool(WAKE_RE.search(text))
    tag = "KEEP" if ok else "drop"
    print("[%s] %.2fs t=%.1f peak=%.2f  %s" % (tag, dur, a / sr, peak, text or "(empty)"), flush=True)
    if ok:
        path = os.path.join(OUT, "%s%03d.wav" % (PREFIX, kept))
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes((seg * 32767).astype("<i2").tobytes())
        kept += 1

print("[done] kept %d / %d pieces -> %s" % (kept, len(pieces), OUT), flush=True)
