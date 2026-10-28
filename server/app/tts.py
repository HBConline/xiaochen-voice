"""TTS reply layer: sherpa-onnx matcha-icefall-zh-baker synthesis + replies.

Replaces the earlier MOSS-TTS-Nano runtime. Measured on the Orange Pi 4 Pro:
MOSS needed ~21s per uncached utterance and ~1GB resident; this matcha model
synthesises in ~0.15s warm with ~150MB resident, which is what makes the voice
assistant feel responsive. The model files live in app/tts-model/ (downloaded
from hf-mirror).

The public interface is unchanged — build_reply(intent) -> {reply_text,
reply_audio, reply_source} — so wakeword_server.py needs no changes.

B3: the per-action template table is gone. make_reply only assembles what the
hardware actually did (执行结果拼装, via voice_brain.speak_results); everything
else — phrasing, refusals, chat — lives in voice_brain.

Heavy deps (sherpa_onnx / numpy / soundfile) import lazily so reply assembly
is testable on a machine with no TTS runtime.
"""
import os
import sys
import threading
import time

MODEL_DIR = os.environ.get(
    "TTS_MODEL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts-model")
)
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts_out")

os.makedirs(OUT_DIR, exist_ok=True)

_tts = None
_tts_lock = threading.Lock()


def _get_tts():
    """Lazy-init the OfflineTts engine (model load ~2s; keep it off startup)."""
    global _tts
    with _tts_lock:
        if _tts is None:
            import sherpa_onnx
            threads = int(os.environ.get("TTS_THREADS", "4"))
            cfg = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    matcha=sherpa_onnx.OfflineTtsMatchaModelConfig(
                        acoustic_model=os.path.join(MODEL_DIR, "model-steps-3.onnx"),
                        vocoder=os.path.join(MODEL_DIR, "hifigan_v2.onnx"),
                        lexicon=os.path.join(MODEL_DIR, "lexicon.txt"),
                        tokens=os.path.join(MODEL_DIR, "tokens.txt"),
                    ),
                    num_threads=threads,
                    provider="cpu",
                ),
                max_num_sentences=1,
            )
            _tts = sherpa_onnx.OfflineTts(cfg)
    return _tts


def warmup():
    """Load the model and run one synthesis so the first real request is fast."""
    try:
        t0 = time.time()
        print("TTS warmup starting...", flush=True)
        tts = _get_tts()
        tts.generate("你好", sid=0, speed=1.0)  # prime caches / threadpool
        if sys.platform.startswith("linux"):
            # Model load leaves short-lived buffers in glibc's arena; hand the
            # pages back so the kernel can account for them (4GB boards).
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        print(f"TTS warmup done in {time.time() - t0:.1f}s", flush=True)
    except Exception as e:
        print(f"TTS warmup failed: {e}", flush=True)



# room -> noun phrase inserted into templates ("客厅的"; none = "")
# (template tables deleted in B3 — phrasings live in voice_brain._summary_line)


def make_reply(intent: dict) -> str | None:
    """Spoken fallback: only what the hardware actually did (执行结果拼装).

    B3 deleted the per-action template table ("好的，打开{r}灯"): it echoed the
    REQUEST regardless of outcome, so a dead device still got "好的，打开客厅
    的灯。" This fallback now assembles from the execution RESULTS the same way
    voice_brain._summary_line does — "客厅灯开好了" / "客厅灯没成，设备没有
    响应" / the refusal detail — so the user never hears a claim the house did
    not deliver. Returns None when nothing was executed: the caller's final
    fallback line ("我在，你说。") beats a canned "收到".
    """
    try:
        import voice_brain
    except Exception:
        return None
    return voice_brain.speak_results(intent)


_REPLY_CACHE: dict = {}   # text -> wav filename (repeated replies skip synthesis)
_REPLY_CACHE_MAX = 64

# Disk cap for tts_out/. The in-memory cache evicts, but every synthesis also
# lands on disk and nothing deleted them — 295 files / 40 MB after a few days,
# and the browser page plays replies straight from this directory. Keep the
# newest KEEP_WAVS files; a just-written file is always kept, and a file whose
# text is still in _REPLY_CACHE can be re-requested, so prune from the oldest
# end only.
_KEEP_WAVS = 100


def _prune_wavs() -> None:
    """Delete the oldest tts_out wavs beyond _KEEP_WAVS. Best-effort."""
    try:
        files = [f for f in os.listdir(OUT_DIR) if f.endswith(".wav")]
        if len(files) <= _KEEP_WAVS:
            return
        paths = [os.path.join(OUT_DIR, f) for f in files]
        paths.sort(key=os.path.getmtime)
        for p in paths[: len(paths) - _KEEP_WAVS]:
            try:
                os.remove(p)
            except OSError:
                pass
    except Exception as e:
        print(f"tts_out prune failed: {e}")


def synthesize(text: str, name: str | None = None) -> str:
    """Synthesize reply text to a wav file; returns the file path.

    Speed notes (measured, CPU-only — target board is Orange Pi 4 Pro):
    - matcha is non-autoregressive: one forward pass, ~0.15s warm for a short
      sentence. No frame cap or seed juggling is needed (that was all MOSS).
    - Identical text reuses the previous wav — free for repeated commands.
    """
    cached = _REPLY_CACHE.get(text)
    if cached and os.path.isfile(os.path.join(OUT_DIR, cached)):
        return os.path.join(OUT_DIR, cached)
    import numpy as np
    import soundfile as sf
    tts = _get_tts()
    if name is None:
        name = f"reply_{int(time.time() * 1000)}.wav"
    out_path = os.path.join(OUT_DIR, name)
    audio = tts.generate(text, sid=0, speed=1.0)
    if audio is None or len(audio.samples) == 0:
        raise RuntimeError(f"TTS produced no audio for {text!r}")
    sf.write(out_path, np.asarray(audio.samples, dtype=np.float32), audio.sample_rate)
    fname = os.path.basename(out_path)
    if len(_REPLY_CACHE) >= _REPLY_CACHE_MAX:
        _REPLY_CACHE.pop(next(iter(_REPLY_CACHE)))
    _REPLY_CACHE[text] = fname
    _prune_wavs()
    return out_path


def build_reply(intent: dict) -> dict:
    """Full reply package: text + synthesized audio URL path (filename only).

    Text source: Qwen3 voice brain when reachable (natural speech), else the
    per-action template fallback so the pipeline never goes silent.
    """
    text = None
    source = "template"
    try:
        import voice_brain
        # source_out tells us whether the text came from the model or from a
        # deterministic rule layer. Without it every non-empty reply was
        # labelled "brain", including _speak_result() confirmations that never
        # call the model (see voice_brain.reply docstring).
        prov: dict = {}
        text = voice_brain.reply(intent.get("text", ""), intent, source_out=prov)
        if text:
            source = prov.get("source", "brain")
    except Exception as e:
        print(f"voice_brain unavailable, template fallback: {e}")
    if not text:
        text = make_reply(intent)
        source = "template"
    if not text:
        # Both voices declined: the brain had nothing and the intent was pure
        # deferral with no device action to phrase. Staying silent beats
        # synthesizing a filler line, but the pipeline contract wants text, so
        # say the one neutral thing that is always true.
        text = "我在，你说。"
        source = "fallback"
    wav_path = synthesize(text)
    return {
        "reply_text": text,
        "reply_audio": os.path.basename(wav_path),
        "reply_source": source,
        # What the hardware layer actually did, per (action, room). The console
        # shows this so a reply that claims success can be checked against the
        # execution result that produced it, rather than trusted on its wording.
        "executed": _executed_detail(intent),
    }


def _executed_detail(intent: dict) -> list:
    """Per-command execution results, or [] when nothing touched a device."""
    try:
        import voice_brain
        out = []
        for (action, room), res in voice_brain._LAST_DEVICE_RESULT.items():
            out.append({
                "action": action,
                "room": room,
                "ok": res.get("ok"),
                "supported": res.get("supported", True),
                "executed": res.get("executed", []),
                "failed": res.get("failed", []),
                "detail": res.get("detail", ""),
            })
        return out
    except Exception:
        return []


if __name__ == "__main__":
    import voice_brain
    voice_brain._LAST_DEVICE_RESULT[("light_on", "living_room")] = {
        "ok": True, "supported": True, "executed": ["living_room"],
        "failed": [], "detail": ""}
    demo = {"action": "light_on", "room": "living_room", "text": "开灯",
            "commands": [{"action": "light_on", "room": "living_room"}]}
    print(make_reply(demo))
    path = synthesize(make_reply(demo), "demo_reply.wav")
    print("saved:", path)
