# -*- coding: utf-8 -*-
"""Generate Chinese speech negatives from TTS.

Product scope: the assistant lives in a Chinese-speaking household, so the
model must NOT fire on ordinary Chinese speech. Official negative packs are
English-heavy; Common Voice zh_CN needs auth + 10GB we do not have. Bridge:
have piper huayan read random everyday Chinese sentences, split them into
3-8s utterance-length clips, and use those as negative spectrograms.

These are synthetic negatives — accent/diversity is narrower than real human
speech — but they cover the failure mode that matters most (ordinary Chinese
phrases of similar length/rhythm to the wake word). Real household recordings
remain the high-value supplement.
"""
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\mww\piper-sample-generator")
from piper_sample_generator.__main__ import generate_samples_onnx

VOICE = r"D:\mww\voices\zh_CN-huayan-medium.onnx"
OUT = Path(r"D:\mww\samples\neg_zh_tts")

# Everyday Chinese sentences: similar length register to wake-word commands,
# household topics, none containing 小宸/小陈/小程/同学 syllable sequence.
SENTENCES = [
    "今天天气不错，我们出去走走。",
    "把桌子收拾一下，一会儿要吃饭了。",
    "明天早上记得叫我起床。",
    "这个电视剧演到哪一集了。",
    "厨房里的水龙头好像没关紧。",
    "晚上想吃面条还是米饭。",
    "快递到了放在门口就行。",
    "把窗户打开透透气。",
    "手机没电了充电器在哪里。",
    "周末我们去看电影吧。",
    "这盆花该浇水了。",
    "垃圾明天早上记得扔。",
    "空调温度调高一点太冷了。",
    "路上的车真多啊。",
    "晚饭做什么菜好呢。",
    "这件衣服洗了没有。",
    "帮我把电视声音关小一点。",
    "楼下新开了一家超市。",
    "明天要降温记得多穿点。",
    "茶几上的报纸拿来给我。",
    "晚饭后出去散散步。",
    "冰箱里的牛奶过期了没有。",
    "今天的工作终于做完了。",
    "晚上早点休息别熬夜。",
    "把阳台的衣服收进来。",
    "这本书看完了借你看。",
    "记得给花施点肥。",
    "下雨了记得带伞。",
    "水果洗好了放在桌上。",
    "灯有点暗换个亮一点的。",
    "闹钟定在六点半。",
    "周末大扫除该开始了。",
    "这汤的味道刚刚好。",
    "门铃响了去看看是谁。",
    "新买的椅子挺舒服的。",
    "记得把药按时吃了。",
    "银行下午几点关门。",
    "明天记得还图书馆的书。",
    "给花草喷点水。",
    "帮我看下这条短信。",
]

TOTAL_TARGET = 600  # ~minutes of Chinese speech at ~4s/clip


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rng = random.Random(2026)
    t0 = time.time()
    need = TOTAL_TARGET  # old collision files (0.wav..) are junk; regenerate all
    print(f"need {need}", flush=True)
    done = 0
    have = 0  # only counting our own renames (old files 0.wav.. are junk)
    tmp_dir = OUT / "_tmp"
    while done < need:
        batch = min(32, need - done)
        texts = [rng.choice(SENTENCES) for _ in range(batch)]
        tmp_dir.mkdir(parents=True, exist_ok=True)
        for f in tmp_dir.glob("*.wav"):
            f.unlink()
        generate_samples_onnx(
            text=texts,
            output_dir=tmp_dir,
            model=VOICE,
            max_samples=batch,
            batch_size=batch,
            length_scales=(0.85, 1.0, 1.15),
            noise_scales=(0.5, 0.667, 0.85, 1.0),
            noise_scale_ws=(0.6, 0.8),
        )
        for i, f in enumerate(sorted(tmp_dir.glob("*.wav"))):
            f.rename(OUT / f"zh_{have + i:05d}.wav")
        have += batch
        done += batch
        if (done // batch) % 10 == 0 or done >= need:
            print(f"  {done}/{need}  ({time.time()-t0:.0f}s)", flush=True)
    print(f"DONE: {done} in {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
