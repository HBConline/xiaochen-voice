# -*- coding: utf-8 -*-
"""Round-3 negatives: same 11 TTS voices as positives, distractor phrases."""
import asyncio
import os
import subprocess

import edge_tts

OUT = r"D:\mww\samples\neg_zh_tts_v2"
os.makedirs(OUT, exist_ok=True)

VOICES = [
    "zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "zh-CN-XiaoyiNeural",
    "zh-CN-YunjianNeural", "zh-CN-YunxiaNeural", "zh-CN-liaoning-XiaobeiNeural",
    "zh-CN-shaanxi-XiaoniNeural", "zh-HK-HiuMaanNeural", "zh-HK-WanLungNeural",
    "zh-TW-HsiaoChenNeural", "zh-TW-YunJheNeural",
]

# 干扰词（结构相同内容不同）+ 日常语（家庭场景）
PHRASES = [
    "小明同学", "小张同学", "小周同学", "小高同学", "小沈同学",
    "小沈铜学", "小沈童鞋", "小张同寝", "小周同学你好", "同学们好",
    "今天天气怎么样", "把灯打开", "把空调打开", "几点了", "我回来啦",
    "今天吃什么", "小宸的下课铃响了", "那阵子同学", "小陈子同窗", "帮我订个闹钟",
]

RATES = ["-15%", "-5%", "+5%", "+10%"]


async def gen():
    sem = asyncio.Semaphore(4)
    tasks = []
    n = 0
    for vi, voice in enumerate(VOICES):
        for pi, phrase in enumerate(PHRASES):
            for ri, rate in enumerate(RATES):
                f = os.path.join(OUT, f"n_v{vi}_w{pi}_r{ri}_{n:04d}.wav")
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
