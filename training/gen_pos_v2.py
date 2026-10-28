# -*- coding: utf-8 -*-
"""Regenerate positive samples with 11 Chinese TTS voices x phrase variants."""
import asyncio
import os
import wave

import edge_tts

OUT = r"D:\mww\samples\pos_xiaochen_v2"
os.makedirs(OUT, exist_ok=True)

VOICES = [
    "zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "zh-CN-XiaoyiNeural",
    "zh-CN-YunjianNeural", "zh-CN-YunxiaNeural", "zh-CN-liaoning-XiaobeiNeural",
    "zh-CN-shaanxi-XiaoniNeural", "zh-HK-HiuMaanNeural", "zh-HK-WanLungNeural",
    "zh-TW-HsiaoChenNeural", "zh-TW-YunJheNeural",
]

# 唤醒词与近音变体（按用户定义：近音全部算正面）
PHRASES = ["小宸同学", "小陈同学", "小程同学", "小呈同学", "小城同学"]

# 每声音每词 3 种语速 x 2 次采样，凑多样性；共 11*5*6 = 330 条
RATES = ["-20%", "-10%", "+0%", "+10%", "+15%", "-5%"]


async def gen():
    sem = asyncio.Semaphore(4)
    tasks = []
    n = 0
    for vi, voice in enumerate(VOICES):
        for pi, phrase in enumerate(PHRASES):
            for ri, rate in enumerate(RATES):
                f = os.path.join(OUT, f"p_v{vi}_w{pi}_r{ri}_{n:04d}.wav")
                n += 1
                if os.path.exists(f):
                    continue

                async def run(f=f, phrase=phrase, voice=voice, rate=rate):
                    async with sem:
                        await edge_tts.Communicate(
                            phrase, voice, rate=rate).save(f)

                tasks.append(run())
    await asyncio.gather(*tasks)
    print(f"[tts] {n} files", flush=True)


asyncio.run(gen())

# convert all mp3-named-wav to real 16k pcm wav
import subprocess
for f in sorted(os.listdir(OUT)):
    if not f.endswith(".wav"):
        continue
    p = os.path.join(OUT, f)
    if open(p, "rb").read(4) != b"RIFF":
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", p,
             "-ar", "16000", "-ac", "1", "-sample_fmt", "s16",
             p + ".c.wav"], check=True)
        os.replace(p + ".c.wav", p)
print("[convert] done", flush=True)
