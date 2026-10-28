# -*- coding: utf-8 -*-
"""离线复跑唤醒模型：PC 上对板子导出的 WAV 跑特征 + streaming 模型，逐帧打印概率。

用来回答"这段音频模型到底看到多少分"。特征用 pymicro_features（和固件同源码），
模型是训练产出的 streaming tflite（50ms 步长，和 feature_step_size=10 对应）。
"""
import sys
import wave

import numpy as np
import tensorflow as tf

TFLITE = ['D:/mww/trained_models/xiaochen_r7/export_quant_int8.tflite']
FEATURE_STRIDE_MS = 10
FRAME_STEP = 3  # streaming 模型 stride=3：每次推理吃连续 3 帧、步进 3 帧（30ms）


def resample22k_to16k(pcm):
    # 模型训练用 22.05k 样本；这里做简易多相/线性重采样只用于 sanity check
    n = int(len(pcm) * 16000 / 22050)
    x = np.arange(n) * 22050 / 16000
    i0 = np.floor(x).astype(int)
    frac = x - i0
    i1 = np.minimum(i0 + 1, len(pcm) - 1)
    return (pcm[i0] * (1 - frac) + pcm[i1] * frac).astype('<i2')


def load_wav(path):
    w = wave.open(path)
    assert w.getnchannels() == 1 and w.getsampwidth() == 2
    sr = w.getframerate()
    a = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2')
    return a, sr


def features(pcm, sr):
    # 训练同源 C 前端（generate_features_for_clip）：10ms 小块 + 按 samples_read
    # 推进。之前 100ms 大块硬灌会丢帧，是恒值假信号的病根。
    from microwakeword.audio.audio_utils import generate_features_for_clip
    return generate_features_for_clip(pcm.astype('<i2'), step_ms=10)


def run_model(tflite_path, feats):
    interp = tf.lite.Interpreter(model_path=tflite_path)
    interp.allocate_tensors()
    det = interp.get_input_details()
    if len(det) > 1:
        print('stateful model inputs:', [(d['name'], list(d['shape']), str(d['dtype'])) for d in det])
    inp = interp.get_input_details()[0]
    q_scale, q_zero = inp['quantization']
    probs = []
    for i in range(0, len(feats) - 3 + 1, FRAME_STEP):
        f = feats[i:i + 3][np.newaxis, :, :]
        q = np.clip(np.round(f / q_scale + q_zero), -128, 127).astype(np.int8)
        interp.set_tensor(inp['index'], q)
        interp.invoke()
        o = interp.get_output_details()[0]
        raw = interp.get_tensor(o['index'])
        oq = o['quantization']
        probs.append(float(raw[0][0]) * oq[0] / 255.0 if oq[0] else float(raw[0][0]))
    return probs


def show(path, tflite_path):
    pcm, sr = load_wav(path)
    if sr != 16000:
        pcm = resample22k_to16k(pcm.astype(np.float32))
        sr = 16000
    feats = features(pcm, sr)
    probs = [v * 255 for v in run_model(tflite_path, feats)]
    # 每 17 次推理 ≈ 0.5s，打印一个最大值便于看时间线
    win = [max(probs[i:i + 17]) for i in range(0, len(probs), 17)]
    print(f'{path}: {len(pcm)/sr:.2f}s feats={feats.shape} max={max(probs) if probs else 0:.3f}')
    print('  0.5s window max:', ' '.join(f'{v:.2f}' for v in win))


if __name__ == '__main__':
    show(sys.argv[1] if len(sys.argv) > 1 else 'D:/esphome-xiaochen/listen.wav', TFLITE[0])
