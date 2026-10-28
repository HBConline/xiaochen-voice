"""Wake word + ASR + Intent server for smart home voice assistant."""
import sys
import os
import json
import base64
import subprocess
import threading
import time

import numpy as np

sys.stdout.reconfigure(encoding='utf-8')

import sherpa_onnx
import soundfile as sf
from flask import Flask, send_from_directory, request, jsonify
from flask_sock import Sock

# Intent understanding
sys.path.insert(0, os.path.dirname(__file__))
from intent import understand as understand_intent
import tts
import voice_state

app = Flask(__name__, static_folder='static')
# Flask's jsonify answers with bare "application/json" — no charset — and a
# browser is then free to decode the body as Latin-1, turning every Chinese
# device name on the panel into mojibake. jsonify already escapes non-ASCII as
# \uXXXX, so the bytes were never wrong; the missing charset declaration was.
# Declare it once here so every JSON route inherits it.
app.json.mimetype = "application/json; charset=utf-8"
sock = Sock(app)

KWS_MODEL_DIR = os.path.join(os.path.dirname(__file__), '..', 'kws', 'sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01-mobile')
KEYWORDS_FILE = os.path.join(os.path.dirname(__file__), '..', 'kws', 'my_keywords.txt')

# KWS model
print("Loading KWS model...")
kws = sherpa_onnx.KeywordSpotter(
    tokens=os.path.join(KWS_MODEL_DIR, 'tokens.txt'),
    encoder=os.path.join(KWS_MODEL_DIR, 'encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx'),
    decoder=os.path.join(KWS_MODEL_DIR, 'decoder-epoch-12-avg-2-chunk-16-left-64.onnx'),
    joiner=os.path.join(KWS_MODEL_DIR, 'joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx'),
    keywords_file=KEYWORDS_FILE,
    num_threads=2,
    provider='cpu',
)
print("KWS loaded.")

# VAD model
print("Loading VAD model...")
vad_config = sherpa_onnx.VadModelConfig()
vad_config.silero_vad.model = os.path.join(os.path.dirname(__file__), '..', 'silero-vad', 'silero_vad.onnx')
vad_config.silero_vad.threshold = 0.5
# Endpoint timing: the reply cannot start until the VAD declares the utterance
# finished, so every 0.1s here is 0.1s of dead air on every interaction.
# 0.5s was overly conservative; our utterances are short commands, so a 0.3s
# tail is enough to avoid clipping mid-sentence pauses.
vad_config.silero_vad.min_silence_duration = 0.3
vad_config.silero_vad.min_speech_duration = 0.15
vad_config.sample_rate = 16000
print("VAD loaded.")

# ASR model
print("Loading SenseVoice model...")
recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=os.path.join(os.path.dirname(__file__), 'model.int8.onnx'),
    tokens=os.path.join(os.path.dirname(__file__), 'tokens.txt'),
    # 'auto' runs a language-ID pass before decoding; every command here is
    # Mandarin, so pin 'zh' and skip it.
    language='zh',
    use_itn=True,
    # 2 threads matches the two big A76 cores; more thrashes (see intent.py).
    num_threads=2,
    provider='cpu',
)
print("ASR loaded.")

WAKE_WORDS = ['小宸同学', '小宸小宸', '你好小宸']

# SenseVoice has no hot-word list (the KWS side does), so the assistant's own
# name comes back spelled however the acoustic model prefers. "小宸" (chén) is
# not among its frequent bigrams and it settles on "小陈" almost every time the
# speaker says the name; "小辰/小晨/小尘" show up on quieter takes. Downstream
# code (voice_brain's vocative table) is keyed on 小宸, so every one of these
# spellings has to be folded back before the text is used — otherwise a bare
# "小宸同学" never matches the table and falls through to a generic reply.
# Only the leading-name position is rewritten: 小陈 is a real surname and a
# mid-sentence "小陈" may belong to an actual person.
_NAME_FIXES = (
    ("小陈", "小宸"),
    ("小辰", "小宸"),
    ("小晨", "小宸"),
    ("小尘", "小宸"),
    ("小成", "小宸"),
)


def normalize_asr_text(text: str) -> str:
    """Fold common ASR misspellings of the wake name back to its canonical form."""
    if not text:
        return text
    for wrong, right in _NAME_FIXES:
        if text == wrong:
            text = right
        elif text.startswith(wrong):
            text = right + text[len(wrong):]
    return text


@app.route('/')
def index():
    return send_from_directory('static', 'wakeword.html')


@app.route('/play')
def play():
    return send_from_directory('static', 'play.html')


@app.route('/test_wavs/<path:filename>')
def test_wav(filename):
    return send_from_directory(
        os.path.join(os.path.dirname(__file__), 'test_wavs'), filename
    )


@app.route('/transcribe', methods=['POST'])
def transcribe():
    if 'audio' not in request.files:
        return jsonify({'error': 'No audio file'}), 400
    file = request.files['audio']
    suffix = os.path.splitext(file.filename)[1] or '.wav'
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name
    try:
        audio, sr = sf.read(tmp_path, dtype='float32')
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        stream = recognizer.create_stream()
        stream.accept_waveform(sr, audio)
        recognizer.decode_stream(stream)
        text = normalize_asr_text(stream.result.text.strip())
        duration = len(audio) / sr

        # Intent understanding
        intent = understand_intent(text)

        resp = {'text': text, 'duration': round(duration, 1), 'intent': intent}
        try:
            resp.update(tts.build_reply(intent))
        except Exception as e:
            resp['reply_error'] = str(e)
        return jsonify(resp)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        os.unlink(tmp_path)


@app.route('/intent', methods=['POST'])
def intent_only():
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'empty text'}), 400
    intent = understand_intent(text)
    resp = {'text': text, 'intent': intent}
    if data.get('speak'):
        try:
            resp.update(tts.build_reply(intent))
        except Exception as e:
            resp['reply_error'] = str(e)
    return jsonify(resp)


@app.route('/state')
def state():
    """Live device state for the pipeline console, grouped for legibility.

    Raw history is one row per (device, room) — an all-rooms sweep writes eight
    rows at once, so the panel showed eighteen near-identical lines and the one
    thing it exists to show (what is on where) was unreadable. Group by device
    class instead: rooms that agree on state AND changed together collapse to a
    single "all rooms" row, and only a genuinely mixed device lists per-room.
    The panel renders `groups`; `devices` is kept for anything still reading the
    flat form.
    """
    try:
        import time
        import device_layer
        import home_config
        ctl = device_layer.get_controller()
        now = time.time()
        rows = []
        for key, on, ts, by in ctl.history():
            dev, _, room = key.partition(":")
            rows.append({
                "device": dev,
                "device_zh": home_config.DEVICE_ZH.get(dev, dev),
                "room": room,
                "room_zh": home_config.ROOM_ZH.get(room, room),
                "on": on,
                "ts": ts,
                "ago": round(now - ts, 1) if ts else None,
                "by": by,
            })
        return jsonify({
            "backend": ctl.backend_name,
            "devices": rows,
            "groups": _group_rows(rows),
        })
    except Exception as e:
        return jsonify({"error": str(e), "devices": [], "groups": []}), 200


# A set of rooms written by one command shares a timestamp within this window;
# beyond it the writes are separate commands and must not be merged.
_SWEEP_WINDOW = 1.5


def _group_rows(rows: list) -> list:
    """Collapse (device, room) rows into ONE line per device class.

    The panel answers "what is on right now", so it is grouped by device, not
    by command: the newest row per class states the headline, and when the class
    is not uniform the exceptions are spelled out beside it ("空调 关（卧室除外:
    开）"). Listing every room separately made eighteen rows of near-identical
    text and hid the one thing worth reading. A class written this instant gets
    `fresh` so the panel can highlight it.
    """
    import time
    import home_config
    now = time.time()
    by_dev: dict = {}
    for r in rows:
        by_dev.setdefault(r["device"], []).append(r)
    groups = []
    for dev, items in by_dev.items():
        items.sort(key=lambda r: (r["ts"] or 0), reverse=True)
        newest = items[0]
        # The state most rooms are in — the headline. Ties keep the newer one.
        on_counts = sum(1 for r in items if r["on"])
        majority = on_counts * 2 >= len(items)
        odd = [r for r in items if bool(r["on"]) != majority]
        # Report the room names that differ, so a single lit bedroom is visible
        # instead of averaged away.
        all_rooms = set(home_config.DEVICE_ROOMS.get(dev) or ())
        uniform = not odd and {r["room"] for r in items} >= all_rooms
        groups.append({
            "device": dev,
            "device_zh": newest["device_zh"],
            "scope": "house" if uniform else "mixed",
            "on": majority,
            "by": newest["by"],
            "ago": newest["ago"],
            "fresh": bool(newest["ts"] and now - newest["ts"] < 5),
            "exceptions": [
                {"room_zh": r["room_zh"], "on": r["on"], "room": r["room"]}
                for r in odd
            ][:4],
        })
    groups.sort(key=lambda g: (g.get("ago") is None, g.get("ago") or 0))
    return groups


@app.route('/session')
def session_state():
    """Dialogue memory: last room/device/action and time of day. Shown on the
    console so a deictic follow-up ("关掉它") is visibly traceable."""
    try:
        import session
        return jsonify(session.snapshot())
    except Exception as e:
        return jsonify({"error": str(e)}), 200


@app.route('/tts_audio/<path:filename>')
def tts_audio(filename):
    return send_from_directory(tts.OUT_DIR, filename)


def _wav_statistics(path: str, sr: int = 16000) -> dict:
    """Level check for a recording: a working mic shows absmax in the hundreds+."""
    audio, r = sf.read(path, dtype='float32')
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if r != sr and len(audio):
        # crude linear resample is fine for a level reading; ASR path in
        # /transcribe passes the file's own sample rate to sherpa instead.
        n = int(len(audio) * sr / r)
        audio = np.interp(np.linspace(0, len(audio) - 1, n),
                          np.arange(len(audio)), audio)
    if not len(audio):
        return {'duration': 0, 'absmax': 0, 'rms': 0}
    return {
        'duration': round(len(audio) / sr, 2),
        'absmax': int(np.abs(audio).max() * 32767),
        'rms': int(np.sqrt((audio.astype(np.float64) ** 2).mean()) * 32767),
    }


# ESP32-S3 nodes record their own follow-up audio (wake runs on-device in a
# microWakeWord loop that can't hand the stream over). Keys live in
# NODE_KEYS="node1:key1,node2:key2"; empty = open (LAN-only deployments).
def _node_ok(device: str, key: str) -> bool:
    spec = os.environ.get('NODE_KEYS', '').strip()
    if not spec:
        return True
    allowed = dict(p.split(':', 1) for p in spec.split(',') if ':' in p)
    return allowed.get(device) == key


@app.route('/node/wake', methods=['POST'])
def node_wake():
    """Node wake fallback over HTTP when MQTT is unavailable: tell it to listen."""
    data = request.get_json(silent=True) or {}
    device = (data.get('device') or '').strip()
    if not device:
        return jsonify({'error': 'device required'}), 400
    if not _node_ok(device, data.get('key', '')):
        return jsonify({'error': 'bad key'}), 403
    voice_state.set_state('listening', ttl=8)
    _log_mic_event('wake', keyword=data.get('wake_word', ''), via='http')
    return jsonify({'listen': True, 'max_sec': 8, 'device': device})


@app.route('/node/audio', methods=['POST'])
def node_audio():
    """Recording from a node: {device,key,audio:b64,format:s16le,sr:16000}.
    Runs the full ASR -> intent -> brain -> TTS chain and answers with the
    reply text plus a wav URL the node can fetch and play."""
    data = request.get_json(silent=True) or {}
    device = (data.get('device') or '').strip()
    if not device:
        return jsonify({'error': 'device required'}), 400
    if not _node_ok(device, data.get('key', '')):
        return jsonify({'error': 'bad key'}), 403
    try:
        sr = int(data.get('sr', 16000))
        raw = base64.b64decode(data.get('audio', ''))
        if data.get('format', 's16le') == 's16le':
            audio = np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0
        else:
            raise ValueError('unsupported format')
    except Exception as e:
        return jsonify({'error': f'bad audio: {e}'}), 400
    if len(audio) < sr * 0.2:
        return jsonify({'error': 'audio too short', 'samples': len(audio)}), 400

    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
        sf.write(tmp.name, audio, sr, subtype='PCM_16')
        tmp_path = tmp.name
    try:
        stats = _wav_statistics(tmp_path, sr)
        if stats['absmax'] < 50:
            return jsonify({'error': 'silent recording', 'levels': stats,
                            'hint': 'check mic wiring / gain on the node'}), 422
        voice_state.set_state('processing', ttl=60)
        stream = recognizer.create_stream()
        stream.accept_waveform(sr, audio)
        recognizer.decode_stream(stream)
        text = normalize_asr_text(stream.result.text.strip())
        _log_mic_event('wake', keyword='', via='node', device=device)
        resp = {'device': device, 'text': text, 'levels': stats}
        if not text:
            return jsonify(resp)
        intent = understand_intent(text)
        resp['intent'] = intent
        try:
            reply = tts.build_reply(intent)
            resp.update(reply)
            if reply.get('reply_audio'):
                resp['reply_url'] = f"/tts_audio/{reply['reply_audio']}"
                # 从 MQTT 分包推给节点喇叭（begin/chunk/end，16k mono）。
                # 不再走板载 aplay：用户听的是节点那边的喇叭。
                wav = os.path.join(tts.OUT_DIR, reply['reply_audio'])
                if os.path.isfile(wav):
                    threading.Thread(target=_push_wav_to_node, args=(wav,), daemon=True).start()
                    voice_state.set_state('speaking', ttl=_wav_duration(wav) + 0.8)
            _log_mic_event('turn', said=text, reply=reply.get('reply_text'),
                           source=reply.get('reply_source'), device=device)
        except Exception as e:
            resp['reply_error'] = str(e)
            _log_mic_event('error', said=text, error=str(e), device=device)
        return jsonify(resp)
    finally:
        os.unlink(tmp_path)


def _log_mic_event(kind: str, **fields) -> None:
    try:
        _mic_event(kind, **fields)
    except Exception:
        pass


def _wav_duration(path: str) -> float:
    """Seconds of audio in a wav; 2.5s guess when unreadable (drives 说话中 TTL)."""
    try:
        import wave
        with wave.open(path, 'rb') as w:
            return w.getnframes() / float(w.getframerate() or 16000)
    except Exception:
        return 2.5


@app.route('/mic_status')
def mic_status():
    """Board-side mic-loop telemetry for the web panel (polled every ~1s)."""
    return jsonify({
        'device': MIC_STATE['device'],
        'state': voice_state.effective_state(),
        'wake_words': WAKE_WORDS,
        'last_wake': MIC_STATE['last_wake'],
        'last_said': MIC_STATE['last_said'],
        'last_reply': MIC_STATE['last_reply'],
        'last_error': MIC_STATE['last_error'],
        'events': MIC_STATE['events'],
    })


@sock.route('/ws')
def handle_websocket(ws):
    """Handle WebSocket: wake word detection -> VAD -> ASR."""
    state = 'waiting_wake'  # waiting_wake -> listening -> processing
    vad = sherpa_onnx.VadModel.create(vad_config)
    buffer = np.array([], dtype=np.float32)
    kws_stream = kws.create_stream()

    ws.send(json.dumps({'type': 'ready', 'state': state, 'wake_words': WAKE_WORDS}))

    while True:
        data = ws.receive()
        if data is None:
            break

        audio = np.frombuffer(data, dtype=np.float32)

        if state == 'waiting_wake':
            # Feed to KWS
            kws_stream.accept_waveform(16000, audio)
            while kws.is_ready(kws_stream):
                kws.decode_stream(kws_stream)
                result = kws.get_result(kws_stream)
                if result:
                    kws.reset_stream(kws_stream)
                    state = 'listening'
                    vad.reset()
                    buffer = np.array([], dtype=np.float32)
                    ws.send(json.dumps({
                        'type': 'wake_detected',
                        'keyword': result,
                        'state': state
                    }))
                    speech_len = 0
                    silence_len = 0
                    break

        elif state == 'listening':
            # Feed to VAD
            chunk_size = vad.window_size()
            for i in range(0, len(audio), chunk_size):
                chunk = audio[i:i + chunk_size]
                if len(chunk) < chunk_size:
                    chunk = np.pad(chunk, (0, chunk_size - len(chunk)))

                # this sherpa_onnx build: is_speech(samples) feeds AND returns state
                in_speech = vad.is_speech(chunk)
                buffer = np.concatenate([buffer, chunk])
                if in_speech:
                    speech_len += len(chunk)
                    silence_len = 0
                else:
                    silence_len += len(chunk)

                if (speech_len > vad.min_speech_duration_samples()
                        and silence_len >= vad.min_silence_duration_samples()):
                    # Silence detected after speech, process
                    state = 'processing'
                    ws.send(json.dumps({'type': 'processing', 'state': state}))

                    stream = recognizer.create_stream()
                    stream.accept_waveform(16000, buffer)
                    recognizer.decode_stream(stream)
                    text = normalize_asr_text(stream.result.text.strip())
                    duration = len(buffer) / 16000

                    # Intent understanding
                    intent = understand_intent(text)

                    ws.send(json.dumps({
                        'type': 'result',
                        'text': text,
                        'duration': round(duration, 1),
                        'intent': intent,
                        'state': 'waiting_wake'
                    }))

                    # TTS reply (after result so intent shows immediately)
                    try:
                        reply = tts.build_reply(intent)
                        reply['type'] = 'reply'
                        ws.send(json.dumps(reply))
                    except Exception as e:
                        ws.send(json.dumps({'type': 'reply', 'reply_error': str(e)}))

                    # Reset to waiting
                    state = 'waiting_wake'
                    buffer = np.array([], dtype=np.float32)
                    kws_stream = kws.create_stream()
                    break

            # Timeout: if listening too long (>10s), reset
            if len(buffer) / 16000 > 10:
                ws.send(json.dumps({'type': 'timeout', 'state': 'waiting_wake'}))
                state = 'waiting_wake'
                buffer = np.array([], dtype=np.float32)
                kws_stream = kws.create_stream()


# Board-side mic-loop telemetry for the web panel. The loop runs in this
# process, so a plain module-level dict is enough — no cross-process state.
MIC_STATE = {
    'device': '',
    'state': 'off',        # off | waiting_wake | listening | processing
    'last_wake': None,     # {'keyword','ts'}
    'last_said': None,     # {'text','ts'}
    'last_reply': None,    # {'text','source','audio','ts'}
    'last_error': None,
    'events': [],          # newest-first ring of finished interactions
}
MIC_EVENTS_MAX = 30


def _mic_event(kind: str, **fields) -> dict:
    """Record one finished interaction and push it onto the ring."""
    ev = {'kind': kind, 'ts': time.time()}
    ev.update(fields)
    MIC_STATE['events'].insert(0, ev)
    del MIC_STATE['events'][MIC_EVENTS_MAX:]
    return ev


def _play_wav(path: str) -> None:
    """Play a wav out the board speaker (ALSA, same card as the mic by default)."""
    backend = os.environ.get('PLAY_BACKEND', 'alsa')
    if backend == 'none':
        return
    if backend == 'http':
        # 节点自己播不了（没有第二路 I2S），先把 wav 发给 PC：
        # PC 端跑 tools/play_reply.py 收 /reply/<name> 再放。
        try:
            import requests
            with open(path, 'rb') as f:
                requests.post(f"{os.environ['PC_PLAY_URL']}/reply/"
                              f"{os.path.basename(path)}", data=f, timeout=30)
        except Exception as e:
            print(f"[play] http push failed: {e}", flush=True)
        return
    dev = os.environ.get('PLAY_DEVICE', os.environ.get('MIC_DEVICE', 'hw:1,0'))
    subprocess.run(['aplay', '-D', dev, '-q', path],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _push_wav_to_node(path: str) -> None:
    """Push reply audio to the node speaker over MQTT (begin/chunk/end, 16k mono).

    Uses the system python3: the venv running this server has no paho-mqtt.
    mqtt_push_tts.py resamples any 8k/16k/22k wav to 16k before chunking.
    """
    push = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mqtt_push_tts.py')
    try:
        r = subprocess.run(['/usr/bin/python3', push, '--wav', path],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            print(f"[push] mqtt_push_tts failed rc={r.returncode}: {r.stderr[-300:]}", flush=True)
    except Exception as e:
        print(f"[push] mqtt push failed: {e}", flush=True)


def mic_loop() -> None:
    """Onboard-mic pipeline: ALSA capture -> KWS wake -> VAD -> ASR -> intent -> TTS out.

    Mirrors the /ws browser pipeline but runs entirely on the board. arecord is
    restarted per interaction so the pipe never backs up during the multi-second
    ASR/TTS phase (stale audio would otherwise flood the next wake window).
    Enabled by MIC_DEVICE (e.g. hw:1,0); disabled when unset.
    """
    mic_dev = os.environ.get('MIC_DEVICE', '')
    if not mic_dev:
        print("mic loop disabled (MIC_DEVICE unset)", flush=True)
        return
    chunk_bytes = 16000 * 2 // 2  # 0.5 s of S16_LE mono @ 16 kHz

    def start_arecord():
        return subprocess.Popen(
            ['arecord', '-D', mic_dev, '-f', 'S16_LE', '-r', '16000',
             '-c', '1', '-t', 'raw', '-q'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    print(f"mic loop on {mic_dev}", flush=True)
    MIC_STATE['device'] = mic_dev
    MIC_STATE['state'] = 'waiting_wake'
    voice_state.set_state('waiting_wake')
    while True:
        arec = start_arecord()
        try:
            kws_stream = kws.create_stream()
            vad = sherpa_onnx.VadModel.create(vad_config)
            state = 'waiting_wake'
            buffer = np.array([], dtype=np.float32)
            speech_len = silence_len = 0

            while True:
                raw = arec.stdout.read(chunk_bytes)
                if not raw or len(raw) < chunk_bytes:
                    print("[mic] arecord ended, restarting", flush=True)
                    break
                audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

                if state == 'waiting_wake':
                    kws_stream.accept_waveform(16000, audio)
                    while kws.is_ready(kws_stream):
                        kws.decode_stream(kws_stream)
                        result = kws.get_result(kws_stream)
                        if result:
                            print(f"[mic] wake: {result}", flush=True)
                            MIC_STATE['state'] = 'listening'
                            voice_state.set_state('listening', ttl=20)
                            MIC_STATE['last_wake'] = {'keyword': result, 'ts': time.time()}
                            _mic_event('wake', keyword=result)
                            state = 'listening'
                            vad.reset()
                            buffer = np.array([], dtype=np.float32)
                            speech_len = silence_len = 0
                            break

                elif state == 'listening':
                    chunk_size = vad.window_size()
                    for i in range(0, len(audio), chunk_size):
                        chunk = audio[i:i + chunk_size]
                        if len(chunk) < chunk_size:
                            chunk = np.pad(chunk, (0, chunk_size - len(chunk)))
                        in_speech = vad.is_speech(chunk)
                        buffer = np.concatenate([buffer, chunk])
                        if in_speech:
                            speech_len += len(chunk)
                            silence_len = 0
                        else:
                            silence_len += len(chunk)
                        if (speech_len > vad.min_speech_duration_samples()
                                and silence_len >= vad.min_silence_duration_samples()):
                            state = 'processing'
                            MIC_STATE['state'] = 'processing'
                            voice_state.set_state('processing', ttl=60)
                            break
                    if state == 'processing':
                        break
                    if len(buffer) / 16000 > 10:
                        print("[mic] listen timeout, back to wake", flush=True)
                        break

            if state != 'processing':
                continue  # fresh arecord + fresh KWS stream

            # Free the capture device before the slow ASR/TTS phase.
            arec.terminate()
            arec.wait()

            stream = recognizer.create_stream()
            stream.accept_waveform(16000, buffer)
            recognizer.decode_stream(stream)
            text = normalize_asr_text(stream.result.text.strip())
            print(f"[mic] said: {text}", flush=True)
            if not text:
                MIC_STATE['state'] = 'waiting_wake'
                voice_state.set_state('waiting_wake')
                continue

            MIC_STATE['last_said'] = {'text': text, 'ts': time.time()}
            intent = understand_intent(text)
            try:
                reply = tts.build_reply(intent)
                print(f"[mic] reply({reply.get('reply_source')}): {reply.get('reply_text')}",
                      flush=True)
                MIC_STATE['last_reply'] = {
                    'text': reply.get('reply_text'),
                    'source': reply.get('reply_source'),
                    'audio': reply.get('reply_audio'),
                    'ts': time.time(),
                }
                wav = os.path.join(tts.OUT_DIR, reply.get('reply_audio') or '')
                if os.path.isfile(wav):
                    voice_state.set_state('speaking', ttl=_wav_duration(wav) + 0.8)
                    _play_wav(wav)
                _mic_event('turn', said=text, intent=intent, reply=MIC_STATE['last_reply'])
            except Exception as e:
                print(f"[mic] reply failed: {e}", flush=True)
                MIC_STATE['last_error'] = str(e)
                _mic_event('error', said=text, error=str(e))
        except Exception as e:
            print(f"[mic] loop error: {e}", flush=True)
            MIC_STATE['last_error'] = str(e)
            time.sleep(1)
        finally:
            MIC_STATE['state'] = 'waiting_wake'
            voice_state.set_state('waiting_wake')
            if arec.poll() is None:
                arec.terminate()


if __name__ == '__main__':
    print("Starting wake word + ASR server on port 8899...")
    print(f"Wake words: {WAKE_WORDS}")
    threading.Thread(target=tts.warmup, daemon=True).start()
    threading.Thread(target=mic_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=8899, debug=False)
