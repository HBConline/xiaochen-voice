# -*- coding: utf-8 -*-
"""Near-miss real recording -> negative clips (round 7).

This take is the user reading the near-miss wordlist (小明同学/小张同学/...).
KEEP any segment with a 同学-family ending EXCEPT the real wake name —
小宸同学/小陈同学 are positives per the user's 近音 rule and must never
enter the negative pool. Everything else in this recording is off-script
and gets dropped.
"""
import glob
import os
import re
import subprocess
import sys
import wave

import numpy as np
import sherpa_onnx
import soundfile as sf

sys.stdout.reconfigure(encoding="utf-8")
APP = r"D:\chenvoice-deploy\app"
OUT = r"D:\mww\samples\neg_nearmiss_real"
TMP = r"D:\mww\tmp\nearmiss_raw.wav"
SRC = sorted(glob.glob(
    r"D:\wxxxdaataa\xwechat_files\wxid_igymei9xkkm822_0c54\msg\file\2026-10\2026年10月25日 11*.m4a"))
assert len(SRC) == 1, SRC

FRAME_MS = 20
THRESH = 0.01
MERGE_GAP_S = 0.25
PAD_S = 0.12
MIN_S, MAX_S = 0.6, 4.0

NEARMISS_RE = re.compile(r"同学|铜学|童鞋|同寝|童鞋|同兮")
WAKE_RE = re.compile(r"小\s*[宸陈辰晨尘成]")

os.makedirs(OUT, exist_ok=True)

rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=os.path.join(APP, "model.int8.onnx"),
    tokens=os.path.join(APP, "tokens.txt"),
    language="zh", use_itn=True, num_threads=2, provider="cpu")


def transcribe(audio, sr):
    st = rec.create_stream()
    st.accept_waveform(sr, audio.astype(np.float32))
    rec.decode_stream(st)
    return st.result.text.strip()


subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", SRC[0],
                "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", TMP], check=True)
audio, sr = sf.read(TMP, dtype="float32")
if audio.ndim > 1:
    audio = audio.mean(axis=1)
print("[src] %s  %.1fs" % (os.path.basename(SRC[0]), len(audio) / sr), flush=True)

frame = sr * FRAME_MS // 1000
n = len(audio) // frame
rms = np.sqrt((audio[: n * frame].reshape(n, frame) ** 2).mean(axis=1))
speech = rms > THRESH
gap_frames = int(MERGE_GAP_S * 1000 / FRAME_MS)

kept = dropped = 0
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
    a = max(0, i * frame - int(PAD_S * sr))
    b = min(len(audio), j * frame + int(PAD_S * sr))
    dur = (b - a) / sr
    if not (MIN_S <= dur <= MAX_S):
        print("[skip] %.2fs" % dur, flush=True)
        i = j
        continue
    seg = audio[a:b]
    text = transcribe(seg, sr)
    if WAKE_RE.search(text):
        print("[drop-wake] %.2fs  %s" % (dur, text), flush=True)
        dropped += 1
    elif NEARMISS_RE.search(text):
        path = os.path.join(OUT, "nm%03d.wav" % kept)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes((seg * 32767).astype("<i2").tobytes())
        print("[keep] %s %.2fs  %s" % (os.path.basename(path), dur, text), flush=True)
        kept += 1
    else:
        print("[drop-off] %.2fs  %s" % (dur, text), flush=True)
        dropped += 1
    i = j

print("[done] kept=%d dropped=%d -> %s" % (kept, dropped, OUT), flush=True)
