# -*- coding: utf-8 -*-
"""Offline A/B scoring: two quant tflites over the same clip groups.

铁律：离线只做相对比较，真机才是裁判。看的是 A/B 相对差与近音负边界，
不拿绝对分数下结论。
"""
import glob
import os
import sys
import wave

import numpy as np

sys.path.insert(0, r"D:\mww\microWakeWord")
from microwakeword.inference import Model

sys.stdout.reconfigure(encoding="utf-8")

MODELS = {
    "r9a": r"D:\mww\trained_models\xiaochen_r9a\export_quant_int8.tflite",
    "r9b": r"D:\mww\trained_models\xiaochen_r9b\export_quant_int8.tflite",
}
GROUPS = {
    "pos_real": r"D:\mww\samples\pos_real",
    "neg_nearmiss_real": r"D:\mww\samples\neg_nearmiss_real",
    "neg_speech_real": r"D:\mww\samples\neg_speech_real",
    "neg_nearmiss_tts": r"D:\mww\samples\neg_nearmiss_tts",
}
GROUP_CAP = {"neg_nearmiss_tts": 40}  # TTS 参考组只抽样


def load_wav(path):
    with wave.open(path, "rb") as w:
        assert w.getsampwidth() == 2
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    if sr != 16000:
        raise ValueError("want 16k: %s" % path)
    return x


def main():
    models = {k: Model(v, stride=3) for k, v in MODELS.items()}
    for g, d in GROUPS.items():
        files = sorted(glob.glob(os.path.join(d, "*.wav")))
        cap = GROUP_CAP.get(g)
        if cap:
            files = files[:cap]
        rows = []
        for f in files:
            pcm = load_wav(f)
            row = {"file": os.path.basename(f)}
            for k, m in models.items():
                preds = m.predict_clip(pcm, step_ms=10)
                row[k] = float(np.max(preds)) if len(preds) else 0.0
            rows.append(row)
        print("\n[%s] n=%d" % (g, len(rows)), flush=True)
        for k in models:
            vals = np.array([r[k] for r in rows])
            print("  %s: mean=%.3f median=%.3f min=%.3f max=%.3f  >=0.17:%d  >=0.5:%d"
                  % (k, vals.mean(), np.median(vals), vals.min(), vals.max(),
                     int((vals >= 0.17).sum()), int((vals >= 0.5).sum())), flush=True)
        if g in ("neg_nearmiss_real", "neg_speech_real"):
            for r in rows:
                print("    %s  r9a=%.3f r9b=%.3f" % (r["file"], r["r9a"], r["r9b"]), flush=True)
        if g == "pos_real":
            weak = [r for r in rows if max(r["r9a"], r["r9b"]) < 0.17]
            print("  弱唤醒条数(max<0.17): %d / %d" % (len(weak), len(rows)), flush=True)


if __name__ == "__main__":
    main()
