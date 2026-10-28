# -*- coding: utf-8 -*-
"""Fine-split the rapid-run span (3.3-9.6s) into single wake-phrase takes.

10ms RMS envelope -> deep valleys (<15% of span speech-median) are cut points;
if a piece is still >2.2s, force-cut at its deepest internal valley. Each
resulting piece is ASR-verified on PC before it can enter pos_real.
"""
import os
import sys
import wave

import numpy as np
import sherpa_onnx
import soundfile as sf

sys.stdout.reconfigure(encoding="utf-8")
APP = r"D:\chenvoice-deploy\app"
RAW = r"D:\mww\tmp\real_raw.wav"
OUT = r"D:\mww\tmp\run_parts"
SPAN = (3.3, 9.6)
FRAME_MS = 10
VALLEY_RATIO = 0.15
PAD_S = 0.12

os.makedirs(OUT, exist_ok=True)
for f in os.listdir(OUT):
    os.remove(os.path.join(OUT, f))

audio, sr = sf.read(RAW, dtype="float32")
if audio.ndim > 1:
    audio = audio.mean(axis=1)
a, b = int(SPAN[0] * sr), int(SPAN[1] * sr)
x = audio[a:b]

frame = sr * FRAME_MS // 1000
n = len(x) // frame
rms = np.sqrt((x[: n * frame].reshape(n, frame) ** 2).mean(axis=1))
active = rms[rms > 0.01]
med = float(np.median(active)) if len(active) else 0.05
print("[env] frames=%d median_rms=%.4f" % (n, med), flush=True)

valleys = rms < VALLEY_RATIO * med
# cut points = valley run centers
cuts = []
i = 0
while i < n:
    if valleys[i]:
        j = i
        while j < n and valleys[j]:
            j += 1
        cuts.append((i + j) // 2)
        i = j
    else:
        i += 1
print("[valleys] at t =", ["%.2f" % (SPAN[0] + c * FRAME_MS / 1000) for c in cuts], flush=True)

bounds = [0] + cuts + [n]
parts = []
for lo, hi in zip(bounds[:-1], bounds[1:]):
    if hi - lo < int(0.6 * 1000 / FRAME_MS):
        continue  # dust; drop or fold — below phrase length
    parts.append((lo, hi))

# force-split pieces still > 2.2s at deepest internal valley
final = []
for lo, hi in parts:
    while (hi - lo) * FRAME_MS / 1000 > 2.2:
        mid_rms = rms[lo + 5: hi - 5]
        if len(mid_rms) == 0:
            break
        cut = lo + 5 + int(np.argmin(mid_rms))
        final.append((lo, cut))
        lo = cut
    final.append((lo, hi))

rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=os.path.join(APP, "model.int8.onnx"),
    tokens=os.path.join(APP, "tokens.txt"),
    language="zh", use_itn=True, num_threads=2, provider="cpu")


def transcribe(wav, srate):
    st = rec.create_stream()
    st.accept_waveform(srate, wav.astype(np.float32))
    rec.decode_stream(st)
    return st.result.text.strip()


idx = 0
for lo, hi in final:
    s = a + max(0, lo * frame - int(PAD_S * sr))
    e = a + min(n * frame, hi * frame + int(PAD_S * sr))
    seg = audio[s:e]
    dur = len(seg) / sr
    text = transcribe(seg, sr)
    name = "run%02d.wav" % idx
    path = os.path.join(OUT, name)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((seg * 32767).astype("<i2").tobytes())
    print("[part] %s %.2fs t=%.2f-%.2f  %s" % (name, dur, s / sr, e / sr, text), flush=True)
    idx += 1
