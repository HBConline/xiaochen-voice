# -*- coding: utf-8 -*-
"""Round 7: TTS near-miss negatives — the 'X同学' region that r6 lacked.

Covers exactly the false-wake phrases from the distractor test, across all
11 voices x 2 rates, so the surname syllable becomes the discriminative cue
again (r2 lesson: negative diversity must match positive diversity).
"""
import asyncio
import os
import subprocess

import edge_tts

OUT = r"D:\mww\samples\neg_nearmiss_tts"
os.makedirs(OUT, exist_ok=True)

VOICES = [
    "zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "zh-CN-XiaoyiNeural",
    "zh-CN-YunjianNeural", "zh-CN-YunxiaNeural", "zh-CN-liaoning-XiaobeiNeural",
    "zh-CN-shaanxi-XiaoniNeural", "zh-HK-HiuMaanNeural", "zh-HK-WanLungNeural",
    "zh-TW-HsiaoChenNeural", "zh-TW-YunJheNeural",
]

PHRASES = [
    "小明同学", "小张同学", "小周同学", "小高同学", "小沈同学",
    "小李同学", "小王同学", "小刘同学", "小杨同学", "小吴同学",
    "小沈铜学", "小沈童鞋", "小张同寝", "小周同学你好", "同学们好",
    "各位同学", "同学你好", "这位同学", "老同学", "张同学",
]
RATES = ["-10%", "+0%"]


async def gen():
    sem = asyncio.Semaphore(4)
    tasks = []
    n = 0
    for vi, voice in enumerate(VOICES):
        for pi, phrase in enumerate(PHRASES):
            for ri, rate in enumerate(RATES):
                f = os.path.join(OUT, f"nm_v{vi}_w{pi}_r{ri}.wav")
                if os.path.exists(f):
                    continue
                n += 1

                async def run(f=f, phrase=phrase, voice=voice, rate=rate):
                    async with sem:
                        await edge_tts.Communicate(phrase, voice, rate=rate).save(f)

                tasks.append(run())
    await asyncio.gather(*tasks)
    print(f"[tts] {n} new files", flush=True)


asyncio.run(gen())

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
