# -*- coding: utf-8 -*-
"""Batch-generate positive wake-word samples with piper zh_CN-huayan (.onnx).

Calls piper_sample_generator.generate_samples_onnx directly, sweeping
length/noise parameters for variety. Writes numbered wavs per word into
D:/mww/samples/pos_*/.

piper native output is 22050 Hz mono; feature extraction resamples to 16k.
"""
import itertools
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\mww\piper-sample-generator")

from piper_sample_generator.__main__ import generate_samples_onnx

VOICE = r"D:\mww\voices\zh_CN-huayan-medium.onnx"
OUT_ROOT = Path(r"D:\mww\samples")

# (word, folder, count) — 近音词全当正样本(产品口径:读音沾边就算)
PLAN = [
    ("小宸同学", "pos_xiaochen", 1500),
    ("小陈同学", "pos_variants", 150),
    ("小程同学", "pos_variants", 150),
    ("小城同学", "pos_variants", 100),
    ("小呈同学", "pos_variants", 100),
    ("小宸铜学", "pos_variants", 100),
    ("小宸小宸", "pos_variants", 150),
    ("你好小宸", "pos_variants", 150),
]

# 4x3x2 = 24 settings, cycled until count reached
LENGTH_SCALES = (0.85, 1.0, 1.15)
NOISE_SCALES = (0.5, 0.667, 0.85, 1.0)
NOISE_SCALE_WS = (0.6, 0.8)

BATCH = 64


def count_wavs(d: Path, prefix: str) -> int:
    """Count this word's own files: variants share a folder, so each word
    gets a filename prefix and counts only its own."""
    return len([f for f in d.iterdir()
                if f.suffix == ".wav" and f.name.startswith(prefix)])


def main() -> None:
    t0 = time.time()
    total = 0
    for word, folder, count in PLAN:
        out_dir = OUT_ROOT / folder
        out_dir.mkdir(parents=True, exist_ok=True)
        prefix = word + '_'
        have = count_wavs(out_dir, prefix)
        need = count - have
        if need <= 0:
            print(f"[skip] {word}: {have} >= {count}")
            continue
        print(f"[gen ] {word}: need {need} more (have {have})", flush=True)
        # generate_samples_onnx writes files itself, numbered from 0 per call,
        # so give it a FRESH subfolder per call and rename files after.
        tmp_dir = out_dir / f"_tmp_{int(time.time())}"
        done = 0
        round_no = 0
        while done < need:
            n = min(BATCH, need - done)
            if tmp_dir.exists():
                for f in tmp_dir.iterdir():
                    f.unlink()
            else:
                tmp_dir.mkdir(parents=True)
            generate_samples_onnx(
                text=word,
                output_dir=tmp_dir,
                model=VOICE,
                max_samples=n,
                batch_size=n,
                length_scales=LENGTH_SCALES,
                noise_scales=NOISE_SCALES,
                noise_scale_ws=NOISE_SCALE_WS,
            )
            # rename into the permanent folder, offset to avoid collisions
            offset = have + done
            for i, f in enumerate(sorted(tmp_dir.glob("*.wav"))):
                f.rename(out_dir / f"{prefix}{offset + i:05d}.wav")
            done += n
            total += n
            round_no += 1
            if round_no % 5 == 0 or done >= need:
                print(f"  {word}: {done}/{need}  (total {total}, "
                      f"{time.time()-t0:.0f}s)", flush=True)
        for f in tmp_dir.iterdir():
            f.unlink()
        tmp_dir.rmdir()
    print(f"ALL DONE: {total} new samples in {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
