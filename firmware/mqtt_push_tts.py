# -*- coding: utf-8 -*-
"""香橙派 TTS → MQTT 分包推给 ESP32 喇叭。
用法（在香橙派上）: python3 mqtt_push_tts.py '你好，我是小宸同学'
也可在 PC 上跑（读本地 wav）: python mqtt_push_tts.py --wav tts16.wav
"""
import base64
import json
import struct
import sys
import time

import paho.mqtt.client as mqtt

TOPIC = "chenvoice/audio/node1"
CHUNK_BYTES = 1024  # 小包：ESP 端每包都要 std::string 拷贝，4KB 连发会打爆内部堆


def load_wav_16k_mono(path):
    import wave
    w = wave.open(path)
    assert w.getsampwidth() == 2, "need 16bit"
    ch, sr, n = w.getnchannels(), w.getframerate(), w.getnframes()
    a = w.readframes(n)
    if ch != 1:
        import array
        v = array.array('h'); v.frombytes(a)
        a = b''.join(struct.pack('<h', (v[i] + v[i+1]) // 2) for i in range(0, len(v) - 1, 2))
    if sr != 16000:
        import array, numpy as np
        v = array.array('h'); v.frombytes(a)
        m = int(len(v) * 16000 / sr)
        v = np.interp(np.linspace(0, len(v) - 1, m), np.arange(len(v)), v).astype('<i2')
        a = v.tobytes()
    return a


def synth_on_orangepi(text):
    """ssh 到香橙派本地跑 TTS；这里假定脚本就在香橙派上执行（--wav 模式除外）。"""
    import subprocess
    r = subprocess.run(
        ["bash", "-c",
         'source ~/chenvoice/.venv/bin/activate && cd ~/chenvoice/app && python - <<PY 2>/dev/null\n'
         'import tts\nprint(tts.synthesize(%r))\nPY' % text],
        capture_output=True, text=True, timeout=60)
    wav = r.stdout.strip().splitlines()[-1]
    print("synthesized:", wav)
    return wav


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--wav":
        # 任意 8k/16k/22.05k mono/stereo 16bit WAV -> 16k mono s16le。
        # 不重采样直接跳 44 字节头会把 22k 的 TTS 按 16k 播成快放怪声。
        pcm = load_wav_16k_mono(sys.argv[2])
    else:
        text = sys.argv[1] if len(sys.argv) > 1 else "你好，喇叭测试"
        wav_path = synth_on_orangepi(text)
        pcm = load_wav_16k_mono(wav_path)

    nsamp = len(pcm) // 2
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        c = mqtt.Client()
    c.connect("127.0.0.1", 1883, 30)
    c.loop_start()
    time.sleep(0.5)
    # Compact JSON on purpose: the node's sscanf parse is strict about spaces.
    c.publish(TOPIC + "/begin", json.dumps({"sr": 16000, "total": nsamp}, separators=(",", ":"))).wait_for_publish()
    time.sleep(0.1)
    sent = 0
    for i in range(0, len(pcm), CHUNK_BYTES):
        c.publish(TOPIC + "/chunk", base64.b64encode(pcm[i:i + CHUNK_BYTES]).decode(), qos=0)
        sent += 1
        time.sleep(0.03)  # 慢推：给 ESP 逐包释放堆留时间
    c.loop_write()  # 冲刷
    time.sleep(0.3)
    c.publish(TOPIC + "/end", "", qos=1).wait_for_publish()
    print(f"pushed {sent} chunks, {nsamp} samples ({nsamp/16000:.1f}s)")
    time.sleep(1)
    c.loop_stop()


if __name__ == "__main__":
    main()
