"""MQTT 桥：唤醒节点 ESP32-S3 -> 香橙派语音管线 -> 回声发给节点播放。

节点只做唤醒（本地 microWakeWord 推理），唤醒后发布 ``chenvoice/wake``：

    chenvoice/wake        {"device":"node1","wake_word":"xiaochen"}
    chenvoice/cmd/node1   节点收到后开始录音并把音频发 HTTP
    chenvoice/audio       ->   {"type":"audio","sr":16000,"b64":...,"n":...,"last":bool}
    chenvoice/ask         ->   {"type":"ask","text":"..."}
    chenvoice/reply       <-   {"text":..., "url":... , "for":"node1"}
    chenvoice/control/node1  <- {"cmd":"listen", "id":...}

设计要点（与语音管线解耦）：
- 只认 paho-mqtt（系统 python3 有装，venv 没有），进程独立；管线挂了不影响唤醒链路。
- 每个唤醒事件开一个 listen 会话：等节点回传音频 -> 拼 wav -> POST /transcribe
  交给管线（ASR + 意图 + 大脑 + TTS）-> 把 reply_audio 的下载地址发回节点。
- 节点拿不到 MQTT 时可用 ``curl "http://<板子>:8899/wake?device=node1&key=..."``
  走 HTTP 同一条路径。
"""
import argparse
import base64
import json
import os
import threading
import time
import urllib.request
import uuid

# paho 只在系统 python3 里装过（apt 的 python3-paho-mqtt），venv 没装也不联网；
# 桥其余依赖（soundfile/numpy）只在 venv 里，所以显式补上系统包路径。
import sys
if '/usr/local/lib/python3.10/dist-packages' not in sys.path:
    sys.path.append('/usr/local/lib/python3.10/dist-packages')
import paho.mqtt.client as mqtt  # noqa: E402


def _post_json(url: str, obj: dict, timeout: float = 120) -> dict:
    req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post_wav(url: str, path: str, timeout: float = 120) -> dict:
    boundary = uuid.uuid4().hex
    with open(path, 'rb') as f:
        data = f.read()
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; '
            f'filename="a.wav"\r\nContent-Type: audio/wav\r\n\r\n')
    body = head.encode() + data + f'\r\n--{boundary}--\r\n'.encode()
    req = urllib.request.Request(url, data=body, headers={
        'Content-Type': f'multipart/form-data; boundary={boundary}'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

BROKER_HOST = os.environ.get('MQTT_HOST', '127.0.0.1')
BROKER_PORT = int(os.environ.get('MQTT_PORT', '1883'))
TOPIC_WAKE = os.environ.get('WAKE_TOPIC', 'chenvoice/wake')
BASE_URL = os.environ.get('VOICE_URL', 'http://127.0.0.1:8899')
TTS_URL = os.environ.get('TTS_URL', BASE_URL)

# 节点最多录 10s、每 0.5s 一包；留足余量，超出即丢弃（迟到包会把旧会话续命）。
AUDIO_IDLE_TIMEOUT = 15.0
# 防抖：误唤醒连发时只跑一次完整问答。
WAKE_DEBOUNCE = 2.0

LOCK = threading.Lock()
SESSION = {'active': None, 'last_wake': 0.0}
LOG = print


def _log(*a):
    LOG(time.strftime('%H:%M:%S'), *a, flush=True)


def _finish_session(sess: dict, reason: str) -> None:
    with LOCK:
        if SESSION['active'] is sess:
            SESSION['active'] = None
    if not sess['chunks']:
        _log(f"session {sess['id']} dropped: {reason}, no audio")
        return
    threading.Thread(target=_run_pipeline, args=(sess,), daemon=True).start()


def _run_pipeline(sess: dict) -> None:
    import numpy as np
    import soundfile as sf

    audio = np.frombuffer(b''.join(sess['chunks']), dtype=np.int16)
    sr = sess['sr']
    secs = len(audio) / sr
    wav_path = f"/tmp/chenvoice_{sess['id']}.wav"
    sf.write(wav_path, audio, sr, subtype='PCM_16')
    _log(f"session {sess['id']}: {secs:.1f}s audio -> {BASE_URL}/transcribe")
    t0 = time.time()
    try:
        res = _post_wav(f"{BASE_URL}/transcribe", wav_path)
    except Exception as e:
        _log(f"transcribe failed: {e}")
        return
    text = res.get('text', '')
    reply_text = res.get('reply_text')
    _log(f"said={text!r} reply({res.get('reply_source')})={reply_text!r} "
         f"{time.time() - t0:.1f}s")
    if reply_text and res.get('reply_audio'):
        url = f"{TTS_URL}/tts_audio/{res['reply_audio']}"
        payload = {'text': reply_text, 'url': url, 'for': sess['device']}
        sess['client'].publish('chenvoice/reply', json.dumps(payload, ensure_ascii=False))


def handle_wake(client, device: str, keyword: str) -> None:
    """唤醒 -> （如果节点不会自己录）下发 listen 指令 + 开新会话。"""
    now = time.time()
    if now - SESSION['last_wake'] < WAKE_DEBOUNCE:
        _log(f"wake from {device} ignored (debounce)")
        return
    SESSION['last_wake'] = now
    sess = {
        'id': uuid.uuid4().hex[:8], 'device': device, 'client': client,
        'chunks': [], 'sr': 16000, 'n': 0,
    }
    with LOCK:
        SESSION['active'] = sess
    _log(f"wake from {device} ({keyword}) -> session {sess['id']}")
    client.publish(f"chenvoice/control/{device}",
                   json.dumps({'cmd': 'listen', 'id': sess['id']}))

    def watchdog():
        while True:
            time.sleep(1.0)
            with LOCK:
                if SESSION['active'] is not sess:
                    return
                idle = time.time() - sess.get('last_rx', now)
            if idle > AUDIO_IDLE_TIMEOUT:
                _finish_session(sess, 'idle timeout')
                return

    threading.Thread(target=watchdog, daemon=True).start()


def handle_audio(client, payload: dict) -> None:
    with LOCK:
        sess = SESSION['active']
    if sess is None:
        _log("audio without session, dropped")
        return
    sess['chunks'].append(base64.b64decode(payload['b64']))
    sess['sr'] = int(payload.get('sr', 16000))
    sess['n'] += 1
    sess['last_rx'] = time.time()
    if payload.get('last'):
        _finish_session(sess, 'end marker')


def handle_ask(client, payload: dict) -> None:
    text = (payload.get('text') or '').strip()
    if not text:
        return
    _log(f"ask: {text!r}")
    try:
        res = _post_json(f"{BASE_URL}/intent", {'text': text, 'speak': True})
    except Exception as e:
        _log(f"ask failed: {e}")
        return
    if res.get('reply_text') and res.get('reply_audio'):
        payload = {'text': res['reply_text'],
                   'url': f"{TTS_URL}/tts_audio/{res['reply_audio']}",
                   'for': 'all'}
        client.publish('chenvoice/reply', json.dumps(payload, ensure_ascii=False))


def on_connect(client, userdata, flags, rc):
    _log(f"mqtt connected rc={rc}")
    client.subscribe([(TOPIC_WAKE, 0), ('chenvoice/audio', 0), ('chenvoice/ask', 0)])


def on_message(client, userdata, msg):
    topic = msg.topic
    try:
        payload = json.loads(msg.payload.decode('utf-8'))
    except Exception:
        payload = {}
    if topic == TOPIC_WAKE:
        handle_wake(client, payload.get('device') or 'node1',
                    payload.get('wake_word', ''))
    elif topic == 'chenvoice/audio':
        handle_audio(client, payload)
    elif topic == 'chenvoice/ask':
        handle_ask(client, payload)


def main():
    global LOG
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default=BROKER_HOST)
    ap.add_argument('--port', type=int, default=BROKER_PORT)
    args = ap.parse_args()
    c = mqtt.Client(client_id='chenvoice-bridge')
    c.on_connect = on_connect
    c.on_message = on_message
    c.reconnect_delay_set(1, 5)
    c.connect(args.host, args.port, keepalive=30)
    c.loop_forever()


if __name__ == '__main__':
    main()
