# -*- coding: utf-8 -*-
"""Daily-speech recordings -> verified real negative clips.

For each source m4a: ffmpeg to 16k mono wav, energy-segment into utterances
(gap 0.25s, pad 0.12s, keep 0.8-6.0s), PC SenseVoice transcribe every piece,
then drop anything that could contain the wake phrase (小X / 同学 / 你好) —
negatives must never contradict the positive set. Clean pieces land in
D:\\mww\\samples\\neg_speech_real\\ as rn_###.wav.
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
OUT = r"D:\mww\samples\neg_speech_real"
TMPDIR = r"D:\mww\tmp"
SRC = sorted(glob.glob(
    r"D:\wxxxdaataa\xwechat_files\wxid_igymei9xkkm822_0c54\msg\file\2026-10\2026年10月25日 09*.m4a"))
assert len(SRC) == 2, SRC

FRAME_MS = 20
THRESH = 0.01
MERGE_GAP_S = 0.25
PAD_S = 0.12
MIN_S, MAX_S = 0.8, 6.0

# wake / near-wake leakage filter (SenseVoice spells 宸 as 陈)
WAKE_RE = re.compile(r"同学|你好|小\s*[宸陈辰晨尘成]")

os.makedirs(OUT, exist_ok=True)
os.makedirs(TMPDIR, exist_ok=True)

rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=os.path.join(APP, "model.int8.onnx"),
    tokens=os.path.join(APP, "tokens.txt"),
    language="zh", use_itn=True, num_threads=2, provider="cpu")


def transcribe(audio, sr):
    st = rec.create_stream()
    st.accept_waveform(sr, audio.astype(np.float32))
    rec.decode_stream(st)
    return st.result.text.strip()


def segments(x, sr):
    frame = sr * FRAME_MS // 1000
    n = len(x) // frame
    rms = np.sqrt((x[: n * frame].reshape(n, frame) ** 2).mean(axis=1))
    speech = rms > THRESH
    gap_frames = int(MERGE_GAP_S * 1000 / FRAME_MS)
    out = []
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
        b = min(len(x), j * frame + int(PAD_S * sr))
        if MIN_S <= (b - a) / sr <= MAX_S:
            out.append((a, b))
        i = j
    return out


kept = dropped = 0
for si, src in enumerate(SRC):
    tmp = os.path.join(TMPDIR, "neg_src%d.wav" % si)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
                    "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", tmp], check=True)
    audio, sr = sf.read(tmp, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    segs = segments(audio, sr)
    print("[src%d] %s  %.1fs  %d segs" % (si, os.path.basename(src), len(audio) / sr, len(segs)),
          flush=True)
    for a, b in segs:
        seg = audio[a:b]
        text = transcribe(seg, sr)
        if WAKE_RE.search(text):
            print("[drop] %.2fs  %s" % (len(seg) / sr, text), flush=True)
            dropped += 1
            continue
        path = os.path.join(OUT, "rn_%03d.wav" % kept)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes((seg * 32767).astype("<i2").tobytes())
        print("[keep] %s %.2fs  %s" % (os.path.basename(path), len(seg) / sr, text), flush=True)
        kept += 1

print("[done] kept=%d dropped=%d -> %s" % (kept, dropped, OUT), flush=True)
