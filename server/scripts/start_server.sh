#!/bin/bash
# Start the voice pipeline server (KWS wake + VAD + SenseVoice ASR + intent + TTS).
# llama-server must already be up (start_llama.sh); the brain is optional at runtime
# (template fallback) but expected for natural replies.
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"

export BRAIN_URL="${BRAIN_URL:-http://127.0.0.1:8080/v1/chat/completions}"
# sherpa-onnx matcha TTS model files live in app/tts-model/ (packaged there,
# not downloaded at runtime). TTS_THREADS: 4 suits the 2 big cores + 2 little.
export TTS_MODEL_DIR="${TTS_MODEL_DIR:-$DIR/app/tts-model}"
export TTS_THREADS="${TTS_THREADS:-4}"
# Onboard mic / speaker on the ES8389 codec (card 1). Set MIC_DEVICE empty to
# disable the board-side capture loop (browser mic only).
export MIC_DEVICE="${MIC_DEVICE-hw:1,0}"
export PLAY_DEVICE="${PLAY_DEVICE-hw:1,0}"
# 节点喇叭没接线期间，回复发到 PC 的 tools/play_reply.py 播放：
#   PLAY_BACKEND=http PC_PLAY_URL=http://<PC地址>:8898 ./start_server.sh
export PLAY_BACKEND="${PLAY_BACKEND-alsa}"
export PC_PLAY_URL="${PC_PLAY_URL-http://127.0.0.1:8898}"
export DEVICE_BACKEND="${DEVICE_BACKEND:-gpio}"
# Do NOT taskset-pin the pipeline; let it use all cores.
source "$DIR/.venv/bin/activate"
cd "$DIR/app"
exec python wakeword_server.py
