# -*- coding: utf-8 -*-
"""Distractor test: generate TTS near-miss / unrelated phrases and check the
quantized streaming model's max probability against cutoff 0.11."""
import asyncio
import os
import sys
import wave

import numpy as np
import edge_tts

sys.path.insert(0, r"D:\mww\microWakeWord")
from microwakeword.inference import Model

OUT = r"D:\mww\distactor_test"
os.makedirs(OUT, exist_ok=True)
MODEL = Model(r"D:\mww\trained_models\xiaochen_r7\export_quant_int8.tflite", stride=1)

# (text, category) — category 期望结果
PHRASES = [
    # 期望唤醒（近音/本词，按用户定义）
    ("小宸同学", "POS-本词"),
    ("小陈同学", "POS-近音"),
    ("小程同学", "POS-近音"),
    ("小呈同学", "POS-近音"),
    ("小城同学", "POS-近音"),
    ("你好小宸", "POS-变体"),
    ("小宸", "POS-短"),
    # 期望不唤醒：其他同学 + 相似结构 + 日常语
    ("小明同学", "NEG-其他同学"),
    ("小张同学", "NEG-其他同学"),
    ("小周同学", "NEG-其他同学"),
    ("小高同学", "NEG-其他同学"),
    ("小沈同学", "NEG-其他同学"),
    ("小沈铜学", "NEG-近音干扰"),
    ("小沈童鞋", "NEG-近音干扰"),
    ("小张同寝", "NEG-近音干扰"),
    ("小周同学你好", "NEG-语序"),
    ("同学们好", "NEG-日常"),
    ("今天天气怎么样", "NEG-日常"),
    ("把灯打开", "NEG-日常"),
    ("我回来啦", "NEG-日常"),
]

VOICES = ["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural"]

async def gen():
    for i, (text, _) in enumerate(PHRASES):
        for vi, voice in enumerate(VOICES):
            f = os.path.join(OUT, f"{i:02d}_{vi}.wav")
            if not os.path.exists(f):
                await edge_tts.Communicate(text, voice, rate="-10%").save(f)
    print("[tts] done", flush=True)

asyncio.run(gen())

# edge-tts writes mp3 despite .wav extension; convert to 16k pcm wav in place
import subprocess
for i in range(len(PHRASES)):
    for vi in range(len(VOICES)):
        f = os.path.join(OUT, f"{i:02d}_{vi}.wav")
        head = open(f, "rb").read(4)
        if head != b"RIFF":
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", f,
                 "-ar", "16000", "-ac", "1", "-sample_fmt", "s16",
                 f + ".pcm16.wav"], check=True)
            os.replace(f + ".pcm16.wav", f)
print("[convert] done", flush=True)


def read_wav(path):
    with wave.open(path, "rb") as w:
        assert w.getframerate() == 16000, f"{path}: {w.getframerate()}Hz"
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data


print(f"\n{'文本':<10} {'类别':<10} {'声音':<6} {'峰值概率':<8} 判定(cutoff 0.11)")
print("-" * 60)
results = {}
for i, (text, cat) in enumerate(PHRASES):
    for vi, voice in enumerate(VOICES):
        f = os.path.join(OUT, f"{i:02d}_{vi}.wav")
        data = read_wav(f)
        preds = MODEL.predict_clip(data, step_ms=10)
        peak = max(preds) if preds else 0.0
        fired = peak > 0.11
        results.setdefault(cat, []).append((fired, peak))
        print(f"{text:<10} {cat:<10} v{vi}    {peak:<8.4f} {'⚠️唤醒' if fired else '否'}")

print("\n===== 汇总 =====")
for cat, rs in results.items():
    n_fire = sum(1 for f, _ in rs if f)
    peaks = [p for _, p in rs]
    print(f"{cat:<14} 唤醒 {n_fire}/{len(rs)}  峰值范围 {min(peaks):.4f}~{max(peaks):.4f}")
